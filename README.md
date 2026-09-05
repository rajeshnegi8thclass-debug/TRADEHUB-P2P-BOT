# TRADEHUB P2P BOT

Features:
- I2C / C2I selection
- Amount entry
- Unique order numbers
- Private P2P tickets
- Vendor Claim Order button
- Vendor Complete Order button
- Completed transaction logging
- Persistent database support

## Railway Variables
DISCORD_TOKEN = Discord bot token
VENDOR_ROLE_ID = ID of your P2P Vendor role
GUILD_ID = ID of your Discord server (recommended)
DATABASE_URL = Railway PostgreSQL URL (recommended for persistent orders)

## Discord permissions
The bot needs View Channels, Send Messages, Read Message History, Embed Links, Manage Channels and Manage Messages.
Its role must be above the P2P Vendor role.

## First setup
Deploy the bot, add the variables, then run `/setup_p2p` in the channel where you want the exchange panel.
The bot creates `P2P ORDERS` and `completed-transactions` automatically.
