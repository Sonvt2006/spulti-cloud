"""Execute one iteration. This module is launched only on the user's VM."""

import json
import runpy
import sys
from pathlib import Path

import spulti


def main() -> None:
    source, request, result, number, total = sys.argv[1:]
    spulti.step_number = int(number)
    spulti.total_steps = int(total)
    spulti.state = json.loads(Path(request).read_text(encoding="utf-8"))
    spulti.encode_state(spulti.state)
    sys.argv = [source]
    sys.path.insert(0, str(Path(source).parent))
    try:
        runpy.run_path(source, run_name="__main__")
    except SystemExit as exc:
        if exc.code is not None and exc.code != 0:
            raise
    if spulti._torch_checkpoint_required or "_spulti_torch" in spulti.state:
        reference = spulti.state.get("_spulti_torch", {})
        if reference.get("epoch") != spulti.step_number:
            raise RuntimeError(
                "Epoch finished without TorchCheckpoint.save(); "
                "progress was not committed"
            )
    Path(result).write_text(spulti.encode_state(spulti.state), encoding="utf-8")


if __name__ == "__main__":
    main()
