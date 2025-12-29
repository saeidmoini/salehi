# Branching and Deploy Rules

- We maintain two branches for separate environments: `agrad` and `salehi`. Each server should run the matching branch.
- When deploying with `update.sh`, the script detects the current branch (`git rev-parse --abbrev-ref HEAD`) and resets to `origin/<branch>`, so keep the server checked out to the right branch.
- For any change, specify which branch to apply and push; features for one environment should stay isolated to its branch.
- Avoid uncommitted changes on the server before running `update.sh`, since it performs a hard reset to the remote branch.
