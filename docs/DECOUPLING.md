# 核与 NoC 的独立运行

核读取静态 Tile trace JSON/JSONL。capture v2 / SSNPU v1 的执行字段继续校验，捕获模式等来源信息可选。手写与解释器生成的输入共用入口。

```python
from api import simulate_core
report = simulate_core('examples/ooo/capture')
assert report['total_cycles'] == 23
```

`models/ssnpu/system.py` 仅装配核组件。核只导入 `runtime.communication` 和 `runtime.memory` 的公共消息，不导入 NoC、路由、时隙编译器或存储后端。`simulate_core` 要求配置不包含 HBM；带通信指令时注入 `comm_backend`，带 WSE SRAM 访问时注入 `memory_backends` 组件映射。缺少后端明确报错。

`examples/communication/fake_communit.py` 是脚本后端，依次发送 DataAccess，等匹配的 DataReply 再推进。`tests/test_decoupling.py` 验证真实核连接假后端的核内准备、读写、完成和消费者唤醒。该假后端无需路由与 Calendar。

NoC 独立验证使用 `examples/communication/streaming.py` 中的 ByteEndpoint 假核。编译器先生成并安装配置，再运行真实 CommUnit。假核提供存储服务并独立核验目标字节和发布条件。远端 SRAM 的假核示例见 `examples/communication/memory.py`。

联合运行使用 `api.simulate` 或 `python -m cli`。通信源计划可先经 `frontend.communication.compile_plan` 保存，再传给 `communication_plan` 装载。详情见 [核与 NoC 接口](COMMUNIT.md)。

所有模式复用 `runtime.runner.Runner` 和下一拍消息队列。组件有定时动作时提供 `next_event`，没有进展、未来事件或在途消息才报停滞。核维护 Tile 生命周期，NoC 按已装载的路由和时隙执行；SRAM 后端负责地址范围、共享端口和访存可见性。
