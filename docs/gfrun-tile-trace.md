# 从 gfrun 捕获 PTO tile trace

讨论记录：2026-11-13。状态：可行性方案，结构化捕获尚待实现。

## 新的负载入口

除了手写扩展 Tile IR 并通过 PyPTO 解释器捕获，我们还可以修改 gfrun，让它执行 SSM 使用的 ELF，记录实际发生的 tile 操作，再交给 Python 模型回放。这尤其适合做两套性能模型的 benchmark 对比：两边从同一个 ELF 出发，减少手工维护两份等价负载的差异。

```text
同一个 ELF
  ├─ gfsim → SSM 性能结果
  └─ gfrun + 捕获插桩 → 紧凑 PTO tile trace → 适配层 → Python 性能模型
```

gfrun 负责功能执行，包括标量计算、地址计算、循环、分支和 tile 数值运算。捕获结果是针对给定输入、沿实际执行路径展开的静态指令流；捕获完毕后，Python 模型根据依赖重新调度和计时。

gfrun 的源码位于 `SuperScalarModel/emulator/`。`main.cpp` 加载 ELF 并推进 `SoftCore::Step()`；`SoftCore.cpp` 分派普通 tile 操作、共享搬运和多 PE 协作；`engine/` 实现具体功能。已有调试日志、动态指令统计和最终 tile 状态导出，可用来核对捕获结果。

## 复用标准紧凑格式

`pto-spec` 已定义 **PTO TileOp macro assembly**。当前目录标记为 PTO 0.59.0、accepted，包含 119 个直接 tile 操作、144 种形式。一行表示一个 tile 操作，合并形状、类型、布局、参与 PE 和操作数等属性。

规范示例：

```text
TADD <Row=8, Col=64, FP32>, T#1, T#2, ->T<2KB>
```

捕获器可以复用这套操作名和字段定义，输出“标准 TileOp 表达 + 运行时元数据”。矩阵尺寸、有效区域和配置属性按照规范解释，执行后已确定的地址和版本另行保存。

| 需要补充的信息 | 用途 |
| --- | --- |
| 动态指令 ID、线程内序号、Core/PE/线程身份 | 区分循环实例及执行上下文 |
| 操作数对应的动态版本、生产者和访问区域 | 建立依赖，处理同名复用、view 和部分写入 |
| 地址、步长及标量操作数的实际值 | 消除对原程序 GPR 状态的依赖 |
| 协作实例、参与集合、同步关系和生命周期信息 | 表达多 PE 与共享 tile 行为 |
| 规范版本和来源身份 | 明确不同工具链及模型版本的语义 |

这意味着可以在已有规范上做局部补充，无需重新设计整套 tile 指令语法。但“格式补充较少”不等于“全指令插桩很少”：普通操作、共享搬运和协作计算走不同执行路径，必须分别覆盖。规范存在也不表示当前 gfrun 已经支持该格式导出，或工具链已经完整支持该宏汇编输入。

生命周期字段需要核对目标规范与实际模型版本。当前宏汇编规范明确说明 `.reuse` 不是 canonical 写法、当前 B.IOT 不释放源；我们 workload 中的 reuse/last-use 约定需要经过适配，不能直接视作所有标准操作已有的属性。

## 捕获范围与实施重点

先覆盖基础 benchmark，再扩展到普通 matmul 和 dense FA：load/store、matmul/acc、逐元素、exp、归约和 cast。格式可以逐步容纳更多操作；能否完整回放还取决于 gfrun 是否支持该组合，以及后端是否有对应语义和 cost。

插桩在操作数和参数已经解析的位置保存输入快照，在操作实际成功执行后确认记录。多 PE 等待、恢复和重试必须去重；共享操作要区分一次物理搬运与多个参与者；捕获器生成独立的动态版本，不能只按寄存器名称关联。

各线程分别保留程序顺序，跨线程只加入有依据的数据及同步关系。gfrun 为功能执行选择的线程先后顺序，不应直接变成性能模型的全局串行约束。未知操作或未完成捕获需要明确标记，避免把缺少指令的文件当作完整负载。

## 当前编译环境的限制

TileOP C++ → ELF 的编译路线已经存在，依赖独立的 Linx 专用 LLVM/Clang 工具链、TileOP API 头文件、链接器和运行库。SuperNPUBench 提供算子代码与构建脚本；SuperScalarModel 提供 gfrun/gfsim；`linx-toolchain-build` 负责构建目标工具链。

当前本机尚未找到并验证可用的这套编译器，`COMPILER_DIR` 未配置。普通宿主 LLVM 不能直接替代该目标工具链。因此，目前可确认的实验入口是仓库中的已有 ELF；编写、修改任意算子再生成新 ELF，还需要取得或构建与模型兼容的工具链。

现阶段先从已有单 PE ELF 打通捕获和回放，再覆盖四 PE/shared 用例。获得工具链后，同一条路径即可接入自行编写的负载。已有基础对比结果见 [首轮基线](benchmarks/ssm-baseline.md) 和 [完整 SSM 运行简报](benchmarks/ssm-native-results.md)。

## 参考位置

- 格式说明：`/home/lc/superscalarNPU/pto-spec/docs/virtual-isa/tileop-macro-assembly.md`
- 机器可读目录：`/home/lc/superscalarNPU/pto-spec/spec/catalog/tile-macro-assembly.json`
- 功能执行入口：`/home/lc/superscalarNPU/SuperScalarModel/emulator/main.cpp`、`emulator/SoftCore.cpp`
- 工具链与算子编译说明：`/home/lc/superscalarNPU/SuperNPUBench/README.md`
