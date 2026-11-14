"""One execution trains one epoch. See ../PYTORCH.md for run/resume commands."""

import random

import torch
from spulti_torch import TorchCheckpoint
from torch import nn

# Stable dataset across processes/VMs, without consuming the training RNG.
torch.set_num_threads(1)
random.seed(42)
torch.manual_seed(42)
data_rng = torch.Generator().manual_seed(123)
x = torch.randn(256, 1, generator=data_rng)
y = 3 * x + 2 + 0.05 * torch.randn(256, 1, generator=data_rng)

model = nn.Linear(1, 1)
optimizer = torch.optim.Adam(model.parameters(), lr=0.05)
checkpoint = TorchCheckpoint(model, optimizer)

# This loop covers batches of ONE epoch, not multiple epochs.
model.train()
order = torch.randperm(len(x))
for indices in order.split(32):
    optimizer.zero_grad()
    loss = nn.functional.mse_loss(model(x[indices]), y[indices])
    loss.backward()
    optimizer.step()

with torch.no_grad():
    mse = nn.functional.mse_loss(model(x), y).item()
checkpoint.save(metrics={"loss": mse})
print(f"epoch={checkpoint.epoch} loss={mse:.6f}")
