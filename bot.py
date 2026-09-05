import os
import asyncio
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "1505193785603395755"))
VENDOR_ROLE_ID = int(os.getenv("VENDOR_ROLE_ID", "1543270258704523345"))
EXCHANGE_CATEGORY_ID = int(os.getenv("EXCHANGE_CATEGORY_ID", "1543227011030450276"))
EXCHANGE_CHANNEL_ID = int(os.getenv("EXCHANGE_CHANNEL_ID", "1543266257204420779"))
HISTORY_CHANNEL_ID = int(os.getenv("HISTORY_CHANNEL_ID", "1543268323318304960"))

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing in Railway Variables.")

DB = "tradehub_p2p.db"
db = sqlite3.connect(DB, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no INTEGER UNIQUE,
    guild_id INTEGER,
    customer_id INTEGER,
    vendor_id INTEGER,
    exchange_type TEXT,
    network TEXT,
    amount TEXT,
    status TEXT,
    channel_id INTEGER,
    created_at TEXT,
    completed_at TEXT
)""")
db.commit()

def next_order():
    row = db.execute("SELECT COALESCE(MAX(order_no), 0) + 1 FROM orders").fetchone()
    return row[0]

def get_order_by_channel(channel_id):
    return db.execute("SELECT * FROM orders WHERE channel_id=?", (channel_id,)).fetchone()

def get_order(order_no):
    return db.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()

def is_vendor(member):
    return member.guild_permissions.administrator or any(r.id == VENDOR_ROLE_ID for r in member.roles)

def row_data(row):
    return {
        "id": row[0], "order_no": row[1], "guild_id": row[2],
        "customer_id": row[3], "vendor_id": row[4], "type": row[5],
        "network": row[6], "amount": row[7], "status": row[8],
        "channel_id": row[9], "created": row[10], "completed": row[11]
    }

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(P2PView())
        self.add_view(OrderView())
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print("TRADEHUB slash commands synced.")

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")

bot = Bot()

class P2PView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="I2C", style=discord.ButtonStyle.success, emoji="🟢", custom_id="p2p:i2c")
    async def i2c(self, interaction, button):
        await interaction.response.send_modal(OrderModal("I2C"))

    @discord.ui.button(label="C2I", style=discord.ButtonStyle.primary, emoji="🔵", custom_id="p2p:c2i")
    async def c2i(self, interaction, button):
        await interaction.response.send_modal(OrderModal("C2I"))

class OrderModal(discord.ui.Modal):
    def __init__(self, exchange_type):
        super().__init__(title=f"{exchange_type} Order")
        self.exchange_type = exchange_type
        self.network = discord.ui.TextInput(
            label="Crypto network",
            placeholder="Example: BNB / TRC20 / ERC20",
            max_length=30
        )
        self.amount = discord.ui.TextInput(
            label="Exchange amount",
            placeholder="Example: 50 USDT",
            max_length=50
        )
        self.add_item(self.network)
        self.add_item(self.amount)

    async def on_submit(self, interaction):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("Server only.", ephemeral=True)

        order_no = next_order()
        customer = interaction.user
        category = guild.get_channel(EXCHANGE_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            category = discord.utils.get(guild.categories, name="P2P Exchange")
        if not category:
            return await interaction.response.send_message(
                "❌ P2P Exchange category not found. Check EXCHANGE_CATEGORY_ID.", ephemeral=True
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            customer: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            )
        }
        vendor_role = guild.get_role(VENDOR_ROLE_ID)
        if vendor_role:
            overwrites[vendor_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            )
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_channels=True, manage_messages=True, embed_links=True
            )

        channel = await guild.create_text_channel(
            f"{self.exchange_type.lower()}-{order_no:04d}",
            category=category,
            overwrites=overwrites
        )

        now = datetime.now(timezone.utc).isoformat()
        db.execute("""INSERT INTO orders
            (order_no,guild_id,customer_id,vendor_id,exchange_type,network,amount,status,channel_id,created_at,completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (order_no, guild.id, customer.id, None, self.exchange_type,
             str(self.network.value), str(self.amount.value), "waiting_vendor",
             channel.id, now, None))
        db.commit()

        embed = discord.Embed(
            title=f"🎫 {self.exchange_type} ORDER #{order_no:04d}",
            description="A P2P Exchanger will claim this ticket and handle the deal.",
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="👤 Customer", value=customer.mention)
        embed.add_field(name="💱 Type", value=self.exchange_type)
        embed.add_field(name="🌐 Network", value=self.network.value)
        embed.add_field(name="💰 Amount", value=self.amount.value)
        embed.add_field(name="📌 Status", value="Waiting for Vendor", inline=False)
        embed.set_footer(text="TRADEHUB P2P")

        await channel.send(customer.mention, embed=embed, view=OrderView())
        await interaction.response.send_message(
            f"✅ Order **#{order_no:04d}** created: {channel.mention}", ephemeral=True
        )

class OrderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Order", style=discord.ButtonStyle.success, emoji="🤝", custom_id="p2p:claim")
    async def claim(self, interaction, button):
        if not isinstance(interaction.user, discord.Member) or not is_vendor(interaction.user):
            return await interaction.response.send_message("❌ P2P Exchanger role required.", ephemeral=True)

        row = get_order_by_channel(interaction.channel.id)
        if not row:
            return await interaction.response.send_message("❌ Order not found.", ephemeral=True)
        data = row_data(row)

        if data["status"] != "waiting_vendor":
            return await interaction.response.send_message("❌ This order is already claimed.", ephemeral=True)

        db.execute("UPDATE orders SET vendor_id=?, status=? WHERE id=?",
                   (interaction.user.id, "in_progress", data["id"]))
        db.commit()

        await interaction.response.edit_message(
            content=interaction.message.content if interaction.message else None,
            embed=discord.Embed(
                title=f"🤝 ORDER #{data['order_no']:04d} — IN PROGRESS",
                description=f"Vendor: {interaction.user.mention}\nCustomer: <@{data['customer_id']}>",
            ).add_field(name="💱 Type", value=data["type"])
             .add_field(name="🌐 Network", value=data["network"])
             .add_field(name="💰 Amount", value=data["amount"]),
            view=OrderView()
        )
        await interaction.channel.send(
            f"🤝 {interaction.user.mention} claimed **Order #{data['order_no']:04d}**."
        )

    @discord.ui.button(label="Complete Order", style=discord.ButtonStyle.primary, emoji="✅", custom_id="p2p:complete")
    async def complete(self, interaction, button):
        if not isinstance(interaction.user, discord.Member) or not is_vendor(interaction.user):
            return await interaction.response.send_message("❌ P2P Exchanger role required.", ephemeral=True)

        row = get_order_by_channel(interaction.channel.id)
        if not row:
            return await interaction.response.send_message("❌ Order not found.", ephemeral=True)
        data = row_data(row)

        if data["status"] != "in_progress":
            return await interaction.response.send_message("❌ Claim the order first.", ephemeral=True)
        if data["vendor_id"] != interaction.user.id and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Only the assigned vendor can complete this.", ephemeral=True)

        completed = datetime.now(timezone.utc).isoformat()
        db.execute("UPDATE orders SET status=?, completed_at=? WHERE id=?",
                   ("completed", completed, data["id"]))
        db.commit()

        history = interaction.guild.get_channel(HISTORY_CHANNEL_ID)
        embed = discord.Embed(title=f"✅ COMPLETED TRANSACTION • #{data['order_no']:04d}",
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="💱 Type", value=data["type"], inline=True)
        embed.add_field(name="🌐 Network", value=data["network"], inline=True)
        embed.add_field(name="💰 Amount", value=data["amount"], inline=True)
        embed.add_field(name="👤 Customer", value=f"<@{data['customer_id']}>", inline=True)
        embed.add_field(name="🏪 Vendor", value=f"<@{interaction.user.id}>", inline=True)
        embed.add_field(name="📌 Status", value="Completed", inline=True)
        if history:
            await history.send(embed=embed)

        await interaction.response.send_message("✅ Order completed and added to P2P History.")
        await asyncio.sleep(3)
        await interaction.channel.delete(reason=f"Completed P2P Order #{data['order_no']:04d}")

    @discord.ui.button(label="Cancel Order", style=discord.ButtonStyle.danger, emoji="✖️", custom_id="p2p:cancel")
    async def cancel(self, interaction, button):
        row = get_order_by_channel(interaction.channel.id)
        if not row:
            return await interaction.response.send_message("❌ Order not found.", ephemeral=True)
        data = row_data(row)

        allowed = interaction.user.id == data["customer_id"] or interaction.user.id == data["vendor_id"]
        if isinstance(interaction.user, discord.Member):
            allowed = allowed or interaction.user.guild_permissions.administrator
        if not allowed:
            return await interaction.response.send_message("❌ You cannot cancel this order.", ephemeral=True)

        db.execute("UPDATE orders SET status=? WHERE id=?", ("cancelled", data["id"]))
        db.commit()
        await interaction.response.send_message("✖️ Order cancelled. Closing ticket...")
        await asyncio.sleep(2)
        await interaction.channel.delete(reason=f"Cancelled P2P Order #{data['order_no']:04d}")

@bot.tree.command(name="p2p", description="Open the TRADEHUB P2P exchange panel.")
async def p2p(interaction):
    embed = discord.Embed(
        title="💱 TRADEHUB P2P EXCHANGE",
        description=(
            "Choose your exchange:\n\n"
            "🟢 **I2C** — INR → Crypto\n"
            "🔵 **C2I** — Crypto → INR\n\n"
            "Select an option, enter the amount and network, and a private ticket will be created."
        )
    )
    embed.set_footer(text="TRADEHUB P2P")
    await interaction.channel.send(embed=embed, view=P2PView())
    await interaction.response.send_message("✅ P2P exchange panel posted.", ephemeral=True)

@bot.tree.command(name="order", description="Check a P2P order.")
@app_commands.describe(order_number="Order number")
async def order(interaction, order_number: int):
    row = get_order(order_number)
    if not row or row[2] != interaction.guild_id:
        return await interaction.response.send_message("❌ Order not found.", ephemeral=True)
    data = row_data(row)
    allowed = interaction.user.id in {data["customer_id"], data["vendor_id"]}
    if isinstance(interaction.user, discord.Member):
        allowed = allowed or interaction.user.guild_permissions.administrator or is_vendor(interaction.user)
    if not allowed:
        return await interaction.response.send_message("❌ You cannot view this order.", ephemeral=True)
    await interaction.response.send_message(
        f"🆔 **#{data['order_no']:04d}**\n"
        f"💱 **Type:** {data['type']}\n"
        f"🌐 **Network:** {data['network']}\n"
        f"💰 **Amount:** {data['amount']}\n"
        f"📌 **Status:** {data['status'].replace('_',' ').title()}\n"
        f"👤 **Customer:** <@{data['customer_id']}>\n"
        f"🏪 **Vendor:** {('<@' + str(data['vendor_id']) + '>') if data['vendor_id'] else 'Not assigned'}",
        ephemeral=True
    )

@bot.tree.command(name="vendor_claim", description="Claim the order in the current ticket.")
async def vendor_claim(interaction):
    if not isinstance(interaction.user, discord.Member) or not is_vendor(interaction.user):
        return await interaction.response.send_message("❌ P2P Exchanger role required.", ephemeral=True)
    row = get_order_by_channel(interaction.channel.id)
    if not row:
        return await interaction.response.send_message("❌ Order not found.", ephemeral=True)
    data = row_data(row)
    if data["status"] != "waiting_vendor":
        return await interaction.response.send_message("❌ Order is already claimed.", ephemeral=True)
    db.execute("UPDATE orders SET vendor_id=?, status=? WHERE id=?",
               (interaction.user.id, "in_progress", data["id"]))
    db.commit()
    await interaction.response.send_message(f"🤝 Order **#{data['order_no']:04d}** claimed by {interaction.user.mention}.")

@bot.tree.command(name="vendor_complete", description="Complete the assigned order.")
async def vendor_complete(interaction):
    if not isinstance(interaction.user, discord.Member) or not is_vendor(interaction.user):
        return await interaction.response.send_message("❌ P2P Exchanger role required.", ephemeral=True)
    row = get_order_by_channel(interaction.channel.id)
    if not row:
        return await interaction.response.send_message("❌ Order not found.", ephemeral=True)
    data = row_data(row)
    if data["status"] != "in_progress" or (data["vendor_id"] != interaction.user.id and not interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("❌ You cannot complete this order.", ephemeral=True)
    await interaction.response.send_message("Use the **Complete Order** button in the ticket to finish the transaction.")

@bot.tree.command(name="vendor_orders", description="Show your active P2P orders.")
async def vendor_orders(interaction):
    if not isinstance(interaction.user, discord.Member) or not is_vendor(interaction.user):
        return await interaction.response.send_message("❌ P2P Exchanger role required.", ephemeral=True)
    rows = db.execute(
        "SELECT order_no, exchange_type, network, amount FROM orders WHERE guild_id=? AND vendor_id=? AND status='in_progress' ORDER BY order_no",
        (interaction.guild_id, interaction.user.id)
    ).fetchall()
    if not rows:
        return await interaction.response.send_message("📋 You have no active orders.", ephemeral=True)
    text = "\n".join(f"**#{r[0]:04d}** • {r[1]} • {r[2]} • {r[3]}" for r in rows)
    await interaction.response.send_message(f"📋 **Your Active Orders**\n{text}", ephemeral=True)

@bot.tree.command(name="vendor_add", description="Add a member as a P2P Exchanger.")
@app_commands.describe(member="Member to add")
@app_commands.checks.has_permissions(manage_guild=True)
async def vendor_add(interaction, member: discord.Member):
    role = interaction.guild.get_role(VENDOR_ROLE_ID)
    if not role:
        return await interaction.response.send_message("❌ VENDOR_ROLE_ID is incorrect.", ephemeral=True)
    await member.add_roles(role, reason="TRADEHUB P2P vendor")
    await interaction.response.send_message(f"✅ {member.mention} added as a P2P Exchanger.")

@bot.tree.command(name="vendor_remove", description="Remove a member from P2P Exchangers.")
@app_commands.describe(member="Member to remove")
@app_commands.checks.has_permissions(manage_guild=True)
async def vendor_remove(interaction, member: discord.Member):
    role = interaction.guild.get_role(VENDOR_ROLE_ID)
    if not role:
        return await interaction.response.send_message("❌ VENDOR_ROLE_ID is incorrect.", ephemeral=True)
    await member.remove_roles(role, reason="TRADEHUB P2P vendor removal")
    await interaction.response.send_message(f"✅ {member.mention} removed from P2P Exchangers.")

bot.run(TOKEN)
