# Out-of-order bypass

Three instructions complete in 6 cycles. PE 0's oldest instruction waits for PE 1; its younger independent instruction issues first.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -m cli examples/ooo/capture --output workspace/ooo/report.json
```
