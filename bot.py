import os
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import BigInteger, DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tradehub")

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///tradehub.db")
VENDOR_ROLE_ID = int(os.getenv("VENDOR_ROLE_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine_kwargs = {"connect_args": {"check_same_thread": False}} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, **engine_kwargs)


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    customer_id: Mapped[int] = mapped_column(BigInteger)
    vendor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exchange_type: Mapped[str] = mapped_column(String(10))
    amount: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="waiting_vendor")
    ticket_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Base.metadata.create_all(engine)


def next_order_number(session: Session) -> int:
    last = session.scalar(select(Order).order_by(Order.order_number.desc()).limit(1))
    return (last.order_number + 1) if last else 1


def get_order_by_channel(channel_id: int) -> Order | None:
    with Session(engine) as session:
        return session.scalar(select(Order).where(Order.ticket_channel_id == channel_id))


def get_order(order_number: int) -> Order | None:
    with Session(engine) as session:
        return session.scalar(select(Order).where(Order.order_number == order_number))


def is_vendor(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    return VENDOR_ROLE_ID != 0 and any(role.id == VENDOR_ROLE_ID for role in member.roles)


def ticket_overwrites(guild: discord.Guild, customer: discord.Member):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        customer: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        ),
    }
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_messages=True, embed_links=True
        )
    if VENDOR_ROLE_ID:
        role = guild.get_role(VENDOR_ROLE_ID)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True
            )
    return overwrites


class ExchangeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose I2C or C2I",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="I2C", description="Item/In-game → Crypto", emoji="🟢", value="I2C"),
                discord.SelectOption(label="C2I", description="Crypto → Item/In-game", emoji="🔵", value="C2I"),
            ],
            custom_id="tradehub:exchange_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AmountModal(self.values[0]))


class ExchangeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ExchangeSelect())


class AmountModal(discord.ui.Modal, title="Create P2P Order"):
    amount = discord.ui.TextInput(
        label="Exchange amount",
        placeholder="Example: $50 or 50 USDT",
        required=True, max_length=100
    )

    def __init__(self, exchange_type: str):
        super().__init__(timeout=300)
        self.exchange_type = exchange_type

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Orders can only be created in a server.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        customer = interaction.user

        with Session(engine) as session:
            order_number = next_order_number(session)
            order = Order(
                order_number=order_number,
                guild_id=guild.id,
                customer_id=customer.id,
                exchange_type=self.exchange_type,
                amount=str(self.amount.value).strip(),
            )
            session.add(order)
            session.commit()

        category = discord.utils.get(guild.categories, name="P2P ORDERS")
        if category is None:
            category = await guild.create_category("P2P ORDERS", reason="TRADEHUB P2P setup")

        channel = await guild.create_text_channel(
            f"{self.exchange_type.lower()}-{order_number:04d}",
            category=category,
            overwrites=ticket_overwrites(guild, customer),
            reason=f"TRADEHUB P2P Order #{order_number:04d}",
        )

        with Session(engine) as session:
            db_order = session.scalar(select(Order).where(Order.order_number == order_number))
            db_order.ticket_channel_id = channel.id
            session.commit()

        embed = discord.Embed(
            title=f"🎫 {self.exchange_type} ORDER #{order_number:04d}",
            description="A P2P vendor can claim this order and handle the deal here.",
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 Customer", value=customer.mention, inline=True)
        embed.add_field(name="💱 Exchange", value=self.exchange_type, inline=True)
        embed.add_field(name="💰 Amount", value=str(self.amount.value).strip(), inline=True)
        embed.add_field(name="📌 Status", value="Waiting for Vendor", inline=False)
        embed.set_footer(text="TRADEHUB P2P")

        await channel.send(
            content=f"{customer.mention} — your order is **#{order_number:04d}**.",
            embed=embed, view=OrderView()
        )
        await interaction.followup.send(
            f"✅ Your **{self.exchange_type}** order **#{order_number:04d}** was created: {channel.mention}",
            ephemeral=True
        )


async def finish_order(interaction: discord.Interaction, order: Order):
    if order.status != "in_progress":
        return await interaction.response.send_message("❌ The order must be claimed first.", ephemeral=True)
    if order.vendor_id != interaction.user.id and not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Only the assigned vendor or an admin can complete this.", ephemeral=True)

    with Session(engine) as session:
        db_order = session.scalar(select(Order).where(Order.id == order.id))
        db_order.status = "completed"
        db_order.completed_at = datetime.now(timezone.utc)
        session.commit()

    embed = discord.Embed(title=f"✅ ORDER #{order.order_number:04d} COMPLETED", timestamp=datetime.now(timezone.utc))
    embed.add_field(name="💱 Type", value=order.exchange_type, inline=True)
    embed.add_field(name="💰 Amount", value=order.amount, inline=True)
    embed.add_field(name="👤 Customer", value=f"<@{order.customer_id}>", inline=True)
    embed.add_field(name="🏪 Vendor", value=f"<@{order.vendor_id}>", inline=True)
    embed.add_field(name="📌 Status", value="Completed", inline=True)

    completed = discord.utils.get(interaction.guild.text_channels, name="completed-transactions")
    if completed:
        await completed.send(embed=embed)

    await interaction.response.send_message("✅ Completed transaction logged. Closing ticket.")
    await asyncio.sleep(2)
    try:
        await interaction.channel.delete(reason=f"TRADEHUB P2P Order #{order.order_number:04d} completed")
    except discord.HTTPException:
        pass


class ClaimButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Claim Order", style=discord.ButtonStyle.success, emoji="🤝", custom_id="tradehub:claim")

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not is_vendor(interaction.user):
            return await interaction.response.send_message("❌ You need the P2P Vendor role.", ephemeral=True)
        order = get_order_by_channel(interaction.channel.id)
        if not order:
            return await interaction.response.send_message("❌ This is not an order ticket.", ephemeral=True)
        if order.status != "waiting_vendor":
            return await interaction.response.send_message("❌ This order is already taken.", ephemeral=True)

        with Session(engine) as session:
            db_order = session.scalar(select(Order).where(Order.id == order.id))
            db_order.vendor_id = interaction.user.id
            db_order.status = "in_progress"
            session.commit()

        embed = discord.Embed(title=f"🤝 ORDER #{order.order_number:04d} CLAIMED")
        embed.add_field(name="🏪 Vendor", value=interaction.user.mention, inline=True)
        embed.add_field(name="💰 Amount", value=order.amount, inline=True)
        embed.add_field(name="📌 Status", value="In Progress", inline=True)
        await interaction.response.edit_message(embed=embed, view=OrderView(allow_claim=False))
        await interaction.channel.send(f"🤝 {interaction.user.mention} claimed this order.")


class CompleteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Complete Order", style=discord.ButtonStyle.primary, emoji="✅", custom_id="tradehub:complete")

    async def callback(self, interaction: discord.Interaction):
        order = get_order_by_channel(interaction.channel.id)
        if not order:
            return await interaction.response.send_message("❌ This is not an order ticket.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not is_vendor(interaction.user):
            return await interaction.response.send_message("❌ You need the P2P Vendor role.", ephemeral=True)
        await finish_order(interaction, order)


class CancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel Order", style=discord.ButtonStyle.danger, emoji="✖️", custom_id="tradehub:cancel")

    async def callback(self, interaction: discord.Interaction):
        order = get_order_by_channel(interaction.channel.id)
        if not order:
            return await interaction.response.send_message("❌ This is not an order ticket.", ephemeral=True)

        allowed = interaction.user.id in {order.customer_id, order.vendor_id} or (
            isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
        )
        if not allowed:
            return await interaction.response.send_message("❌ You cannot cancel this order.", ephemeral=True)

        with Session(engine) as session:
            db_order = session.scalar(select(Order).where(Order.id == order.id))
            db_order.status = "cancelled"
            db_order.cancelled_at = datetime.now(timezone.utc)
            session.commit()

        await interaction.response.send_message(f"✖️ Order **#{order.order_number:04d}** cancelled. Closing ticket.")
        await asyncio.sleep(2)
        try:
            await interaction.channel.delete(reason=f"TRADEHUB P2P Order #{order.order_number:04d} cancelled")
        except discord.HTTPException:
            pass


class OrderView(discord.ui.View):
    def __init__(self, allow_claim=True):
        super().__init__(timeout=None)
        if allow_claim:
            self.add_item(ClaimButton())
        self.add_item(CompleteButton())
        self.add_item(CancelButton())


class TradeHubBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(ExchangeView())
        self.add_view(OrderView())
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)


bot = TradeHubBot()


@bot.tree.command(name="setup_p2p", description="Create the TRADEHUB P2P exchange panel.")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_p2p(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)

    category = discord.utils.get(interaction.guild.categories, name="P2P ORDERS")
    if category is None:
        category = await interaction.guild.create_category("P2P ORDERS", reason="TRADEHUB P2P setup")

    completed = discord.utils.get(interaction.guild.text_channels, name="completed-transactions")
    if completed is None:
        completed = await interaction.guild.create_text_channel("completed-transactions", reason="TRADEHUB P2P setup")

    embed = discord.Embed(
        title="💱 TRADEHUB P2P EXCHANGE",
        description=(
            "Choose your exchange:\n\n"
            "🟢 **I2C** — Item/In-game → Crypto\n"
            "🔵 **C2I** — Crypto → Item/In-game\n\n"
            "Select an option below, enter the amount, and a private ticket will be created."
        ),
    )
    embed.set_footer(text="TRADEHUB P2P • Every order gets a unique number")
    await interaction.channel.send(embed=embed, view=ExchangeView())
    await interaction.response.send_message(
        f"✅ Panel created. Order category: `{category.name}` • Completed log: {completed.mention}",
        ephemeral=True
    )


@bot.tree.command(name="order", description="Check a TRADEHUB P2P order.")
@app_commands.describe(order_number="Order number, e.g. 1")
async def order_command(interaction: discord.Interaction, order_number: int):
    order = get_order(order_number)
    if not order or order.guild_id != interaction.guild_id:
        return await interaction.response.send_message("❌ Order not found.", ephemeral=True)

    member = interaction.user
    allowed = member.id in {order.customer_id, order.vendor_id}
    if isinstance(member, discord.Member):
        allowed = allowed or member.guild_permissions.manage_guild or is_vendor(member)
    if not allowed:
        return await interaction.response.send_message("❌ You don't have access to this order.", ephemeral=True)

    await interaction.response.send_message(
        f"🆔 **#{order.order_number:04d}**\n"
        f"💱 **Type:** {order.exchange_type}\n"
        f"💰 **Amount:** {order.amount}\n"
        f"📌 **Status:** {order.status.replace('_',' ').title()}\n"
        f"👤 **Customer:** <@{order.customer_id}>\n"
        f"🏪 **Vendor:** {f'<@{order.vendor_id}>' if order.vendor_id else 'Not assigned'}",
        ephemeral=True
    )


@bot.tree.command(name="vendor_claim", description="Claim the P2P order in the current ticket.")
async def vendor_claim(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_vendor(interaction.user):
        return await interaction.response.send_message("❌ You need the P2P Vendor role.", ephemeral=True)
    order = get_order_by_channel(interaction.channel.id)
    if not order or order.status != "waiting_vendor":
        return await interaction.response.send_message("❌ No available order in this ticket.", ephemeral=True)

    with Session(engine) as session:
        db_order = session.scalar(select(Order).where(Order.id == order.id))
        db_order.vendor_id = interaction.user.id
        db_order.status = "in_progress"
        session.commit()

    await interaction.response.send_message(f"🤝 Order **#{order.order_number:04d}** claimed by {interaction.user.mention}.")


@bot.tree.command(name="vendor_complete", description="Complete the assigned P2P order.")
async def vendor_complete(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_vendor(interaction.user):
        return await interaction.response.send_message("❌ You need the P2P Vendor role.", ephemeral=True)
    order = get_order_by_channel(interaction.channel.id)
    if not order:
        return await interaction.response.send_message("❌ This is not an order ticket.", ephemeral=True)
    await finish_order(interaction, order)


@bot.tree.command(name="vendor_orders", description="Show your active P2P orders.")
async def vendor_orders(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_vendor(interaction.user):
        return await interaction.response.send_message("❌ You need the P2P Vendor role.", ephemeral=True)

    with Session(engine) as session:
        orders = session.scalars(
            select(Order).where(
                Order.guild_id == interaction.guild_id,
                Order.vendor_id == interaction.user.id,
                Order.status == "in_progress"
            ).order_by(Order.order_number)
        ).all()

    if not orders:
        return await interaction.response.send_message("You have no active orders.", ephemeral=True)

    await interaction.response.send_message(
        "📋 **Your Active Orders**\n" +
        "\n".join(f"**#{o.order_number:04d}** • {o.exchange_type} • {o.amount}" for o in orders),
        ephemeral=True
    )


@bot.tree.command(name="vendor_add", description="Give a member the P2P Vendor role.")
@app_commands.describe(member="Member to add as vendor")
@app_commands.checks.has_permissions(manage_guild=True)
async def vendor_add(interaction: discord.Interaction, member: discord.Member):
    if VENDOR_ROLE_ID == 0:
        return await interaction.response.send_message("❌ Set VENDOR_ROLE_ID in Railway Variables first.", ephemeral=True)
    role = interaction.guild.get_role(VENDOR_ROLE_ID)
    if not role:
        return await interaction.response.send_message("❌ Vendor role not found.", ephemeral=True)
    await member.add_roles(role, reason="TRADEHUB P2P vendor")
    await interaction.response.send_message(f"✅ {member.mention} is now a P2P vendor.", ephemeral=True)


@bot.tree.command(name="vendor_remove", description="Remove the P2P Vendor role from a member.")
@app_commands.describe(member="Member to remove as vendor")
@app_commands.checks.has_permissions(manage_guild=True)
async def vendor_remove(interaction: discord.Interaction, member: discord.Member):
    if VENDOR_ROLE_ID == 0:
        return await interaction.response.send_message("❌ Set VENDOR_ROLE_ID in Railway Variables first.", ephemeral=True)
    role = interaction.guild.get_role(VENDOR_ROLE_ID)
    if not role:
        return await interaction.response.send_message("❌ Vendor role not found.", ephemeral=True)
    await member.remove_roles(role, reason="TRADEHUB P2P vendor removal")
    await interaction.response.send_message(f"✅ {member.mention} is no longer a P2P vendor.", ephemeral=True)


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        msg = "❌ You need Manage Server permission."
    else:
        log.exception("Command error", exc_info=error)
        msg = "❌ Something went wrong. Check Railway logs."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


bot.run(TOKEN)
