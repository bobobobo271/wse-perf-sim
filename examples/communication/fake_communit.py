"""Scripted peer for core tests; consumes the same v1 contract as CommUnit.

script(submission, peer) yields DataAccess. It resumes only after the matching
access ID/event returns. This fake has no routing or Calendar implementation.
"""
from runtime.component import Component
from runtime.communication import CommSubmit, Accepted, Completion, DataReply


class FakeCommUnit(Component):
    def __init__(self, script):
        super().__init__('communit')
        self.script, self.jobs, self.history = script, {}, []

    def proceed(self, token, reply=None):
        sub, steps, pending = self.jobs[token]
        if reply and (reply.access_id, reply.event) != pending:
            raise ValueError('Unexpected data reply')
        try:
            command = next(steps) if reply is None else steps.send(reply)
        except StopIteration:
            self.send(sub.endpoint, Completion(token, 'success'))
            del self.jobs[token]
        else:
            if command.token != token:
                raise ValueError('Script token differs from request')
            event = dict(prepare='prepared', read='read_done', write='write_done', publish='published')[command.kind]
            self.jobs[token] = sub, steps, (command.access_id, event)
            self.send(sub.endpoint, command)

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            self.history.append((cycle, message))
            if isinstance(message, CommSubmit):
                token = message.request.request_id
                if token in self.jobs:
                    raise ValueError('Duplicate test request')
                self.jobs[token] = message, iter(self.script(message, self)), None
                self.send(message.endpoint, Accepted(token))
                self.proceed(token)
            elif isinstance(message, DataReply):
                if message.event == 'fault':
                    sub, _, _ = self.jobs.pop(message.token)
                    self.send(sub.endpoint, Completion(message.token, 'fault', str(message.detail), False))
                else:
                    self.proceed(message.token, message)
            else:
                raise TypeError(message)

    def is_idle(self):
        return super().is_idle() and not self.jobs
