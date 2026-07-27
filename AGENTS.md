# Project rules

This is a cryptocurrency trading bot.

## Safety

- Never expose or print API keys, secrets, tokens, or credentials.
- Never modify .env.
- Never read or display secret values.
- Never execute live trading commands.
- Never place, cancel, or modify real exchange orders.
- Never run scripts against production exchange accounts.
- Never enable withdrawal permissions.
- Use mocks or testnet for exchange integration tests.

## Development

- Before changing code, understand the complete execution path.
- Prefer small changes over large rewrites.
- Preserve existing behavior unless explicitly requested.
- Add tests for bug fixes.
- Run tests after modifications.
- Show the diff after every logical change.
- Do not silently change trading logic.

## Trading logic

Changes affecting:
- position sizing
- leverage
- stop loss
- take profit
- order execution
- liquidation calculations
- risk management

must be explained before implementation.