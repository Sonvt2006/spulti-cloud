"""Optional state API inside a file executed by ``spulti-agent submit``.

Each invocation gets the last successful iteration's JSON state. Mutate this
dictionary to checkpoint application results; ordinary Python globals reset.
"""

import json
import math

MAX_STATE_BYTES = 8 * 1024
step_number: int = 0  # 1-based; initialized by script_worker before user code.
total_steps: int = 0
state: dict = {}
_torch_checkpoint_required = False  # Set by the optional epoch checkpoint helper.


def encode_state(value: dict) -> str:
    """Reject lossy/non-JSON values before an iteration can be acknowledged."""

    def validate(item, depth=0):
        if depth > 64:
            raise ValueError("State is nested too deeply")
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("State keys must be strings")
                validate(child, depth + 1)
        elif type(item) is list:
            for child in item:
                validate(child, depth + 1)
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("State numbers must be finite")
        elif type(item) not in (str, int, bool, type(None)):
            raise ValueError("State must contain only JSON values")

    if type(value) is not dict:
        raise ValueError("spulti.state must be a JSON object")
    validate(value)
    encoded = json.dumps(value, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("spulti.state must not exceed 8 KiB")
    return encoded
