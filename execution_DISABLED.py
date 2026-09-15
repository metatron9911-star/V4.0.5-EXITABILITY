"""V4 PAPER build.
Real signing/execution is intentionally absent.
V4.1 can add a Raydium/Jupiter execution adapter only after PAPER acceptance criteria are met.
Never store a seed phrase/private key in GitHub. Use Railway secrets and a dedicated low-balance trading wallet.
"""

def buy(*args, **kwargs):
    raise RuntimeError("AUTO trading is physically disabled in V4 PAPER")

def sell(*args, **kwargs):
    raise RuntimeError("AUTO trading is physically disabled in V4 PAPER")
