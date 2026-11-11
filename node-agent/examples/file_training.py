"""Resume a long training-like invocation from a real file (no ML dependencies).

Submit with --total-steps 1 --step-delay 0 --checkpoint-dir PATH.
A real trainer should similarly save model + optimizer + epoch/RNG state.
"""

import json
import os
import time
from pathlib import Path

root = Path(os.environ["SPULTI_CHECKPOINT_DIR"])
path = root / "training.json"
saved = json.loads(path.read_text()) if path.exists() else {"epoch": 0, "weight": 0}
print(f"TRAINING_RESTORE epoch={saved['epoch']}", flush=True)
for epoch in range(saved["epoch"] + 1, 121):
    time.sleep(1)
    saved = {"epoch": epoch, "weight": saved["weight"] + epoch**2}
    temporary = root / "training.tmp"
    with temporary.open("w") as output:
        json.dump(saved, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    print(f"TRAINING_EPOCH={epoch}", flush=True)
print("TRAINING_COMPLETE", flush=True)
