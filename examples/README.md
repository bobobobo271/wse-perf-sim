# Workload examples and archived results

| Folder | Instructions | Cycles | Contents |
|---|---:|---:|---|
| [matmul](matmul/) | 168 | 7001 | Workload generator, capture, IR, verified report |
| [fa](fa/) | 320 | 1910 | Workload generator, capture, IR, verified report |
| [validation](validation/) | — | — | Regression log, replay summary, validation notes |
| [communication](communication/) | — | parameterized | Standalone byte driver for the request/data/completion interface |
| [decode](decode/) | parameterized | parameterized | PTO capture replay, optional single-core timing comparison and pipeline plots |

All numbers use the synthetic logic-validation configuration. Replaying a capture requires only Python 3.11+ and the standard library. Regenerating matmul/FA captures additionally requires pypto with the SSNPU Builder/TileInterpreter interfaces. Existing trace files are kept byte-for-byte for report hash verification. New run outputs belong in `workspace/`.
