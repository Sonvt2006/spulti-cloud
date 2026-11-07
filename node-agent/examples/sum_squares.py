"""Submit: spulti-agent submit sum_squares.py --total-steps 100"""

from spulti import state, step_number

# This entire file executes once per agent iteration. Results in `state`
# survive the next iteration and are restored from the latest checkpoint.
state["running_sum"] = state.get("running_sum", 0) + step_number**2
print(f"step={step_number} sum={state['running_sum']}")
