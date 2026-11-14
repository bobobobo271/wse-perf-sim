"""Core-side token adapter. It knows bindings and instructions, not Calendar."""
from collections import Counter

from runtime.component import Component
from runtime.protocol import Dispatch, Complete, Consumed, TileDataAccess, ReleaseTileStream, core_name
from runtime.communication import Accepted, Rejected, Completion, DataAccess, DataReply, CommSubmit, request_from_dict
from errors import ModelError


class CommPort(Component):
    def __init__(self, name, instructions):
        super().__init__(name)
        self.nodes = instructions
        self.bindings, self.by_instruction = {}, {}
        self.waiting, self.finished, self.retry = set(), {}, {}
        self.waits = Counter()
        self.completed_tokens = set()
        for key, instruction in instructions.items():
            token = instruction.raw['communication']['token']
            binding = self.bindings.setdefault(token, {})
            binding[instruction.raw['ir_op'].split('.')[1]] = instruction
            raw = instruction.raw['communication']['request']
            binding['request'] = CommSubmit(instruction.core, name, request_from_dict(raw))
            self.by_instruction[key] = token

    def finish(self, instruction):
        self.observe(instruction.id, complete=self.cycle, state='complete')
        self.send(core_name(instruction.core, 'rob'), Complete(instruction.id))

    def tick(self, cycle):
        for _, message in self.inputs(cycle):
            if isinstance(message, Dispatch):
                instruction = self.nodes[message.instruction]
                token = self.by_instruction[instruction.id]
                self.observe(instruction.id, received=cycle, state='communication_pending')
                if instruction.raw['ir_op'] == 'comm.submit':
                    self.send('communit', self.bindings[token]['request'])
                else:
                    self.waiting.add(token)
            elif isinstance(message, Accepted):
                binding = self.bindings[message.token]
                self.retry.pop(message.token, None)
                self.observe(binding['submit'].id, accepted=cycle)
                self.finish(binding['submit'])
                # Source retention ends on data-read completion, not acceptance.
            elif isinstance(message, Rejected):
                if not message.retryable:
                    raise ModelError(f'Communication request {message.token} rejected: {message.reason}')
                self.retry[message.token] = cycle+1
                self.waits[self.bindings[message.token]['submit'].id] += 1
            elif isinstance(message, Completion):
                if message.status != 'success':
                    raise ModelError(f'Communication {message.token} fault (bindings quarantined): {message.error}')
                if message.token in self.completed_tokens:
                    raise ModelError('Duplicate communication completion')
                binding = self.bindings[message.token]
                submit = binding['submit']
                self.send(core_name(submit.core, 'tile'), ReleaseTileStream(submit.id))
                self.completed_tokens.add(message.token)
                self.finished[message.token] = message
            elif isinstance(message, DataAccess):
                binding = self.bindings[message.token]
                submit, wait = binding['submit'], binding['wait']
                if message.kind == 'read':
                    binding.setdefault('reads', {})[message.access_id] = message.nbytes
                self.send(core_name(submit.core, 'tile'), TileDataAccess(submit.id, wait.id, message))
            elif isinstance(message, DataReply):
                if message.event == 'read_done':
                    binding = self.bindings[message.token]
                    binding['read_bytes'] = binding.get('read_bytes', 0) + binding['reads'].pop(message.access_id)
                    layout = binding['request'].request.send.layout
                    if binding['read_bytes'] == layout.row_count * layout.row_bytes:
                        self.observe(binding['submit'].id, source_read_complete=cycle)
                self.send('communit', message)
            else:
                raise TypeError(message)
        for token, when in list(self.retry.items()):
            if when <= cycle:
                self.send('communit', self.bindings[token]['request'])
                del self.retry[token]
        for token in sorted(self.waiting & self.finished.keys()):
            wait = self.bindings[token]['wait']
            self.finish(wait)
            self.send(core_name(wait.core, 'tile'), Consumed(wait.id))
            self.waiting.remove(token)
            del self.finished[token]

    def next_event(self):
        return min(self.retry.values(), default=None)

    def is_idle(self):
        return super().is_idle() and not (self.retry or self.waiting or self.finished)

    def snapshot(self):
        return dict(super().snapshot(), waiting=sorted(self.waiting), completed=sorted(self.finished), retry=dict(self.retry))
