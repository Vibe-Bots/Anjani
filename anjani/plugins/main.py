import asyncio
import re
from hashlib import sha256
from typing import TYPE_CHECKING, Any, ClassVar, List, Optional

from aiopath import AsyncPath
from bson.binary import Binary
from pyrogram.enums.chat_type import ChatType
from pyrogram.enums.parse_mode import ParseMode
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    MessageDeleteForbidden,
    MessageNotModified,
)
from pyrogram.raw.functions.updates.get_state import GetState
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from anjani import command, filters, listener, plugin, util

if TYPE_CHECKING:
    from .rules import Rules


class Main(plugin.Plugin):
    """Bot main Commands"""

    name: ClassVar[str] = "Main"

    bot_name: str
    db: util.db.AsyncCollection

    async def on_load(self) -> None:
        self.db = self.bot.db.get_collection("SESSION")

    async def on_start(self, _: int) -> None:
        self.bot_name = (
            self.bot.user.first_name + self.bot.user.last_name
            if self.bot.user.last_name
            else self.bot.user.first_name
        )

        restart = await self.db.find_one({"_id": 5})
        if restart is not None:
            rs_time: Optional[int] = restart.get("time")
            rs_chat_id: Optional[int] = restart.get("status_chat_id")
            rs_message_id: Optional[int] = restart.get("status_message_id")

            # Delete data first in case message editing fails
            await self.db.delete_one({"_id": 5})

            # Bail out if we're missing necessary values
            if rs_chat_id is None or rs_message_id is None or rs_time is None:
                return

            duration = util.time.usec() - rs_time
            duration_str = util.time.format_duration_us(duration)
            __, status_msg = await asyncio.gather(
                self.bot.log_stat("downtime", value=duration),
                self.bot.client.get_messages(rs_chat_id, rs_message_id),
            )
            if isinstance(status_msg, List):
                status_msg = status_msg[0]

            self.bot.log.info(f"Bot downtime {duration_str}")
            await self.send_to_log(
                f"Bot downtime {duration_str}.", reply_to_message_id=status_msg.id
            )
            try:
                await status_msg.delete()
            except MessageDeleteForbidden:
                pass
        else:
            await self.send_to_log("Starting system...")

    async def on_stop(self) -> None:
        async with asyncio.Lock():
            file = AsyncPath("anjani/anjani.session")
            if not await file.exists():
                return

            data = await self.bot.client.invoke(GetState())
            await self.db.update_one(
                {"_id": sha256(self.bot.config.BOT_TOKEN.encode()).hexdigest()},
                {
                    "$set": {
                        "session": Binary(await file.read_bytes()),
                        "date": data.date,
                        "pts": data.pts,
                        "qts": data.qts,
                        "seq": data.seq,
                    }
                },
                upsert=True,
            )

            status_msg = await self.send_to_log("Shutdowning system...")
            self.bot.log.info("Preparing to shutdown...")
            if not status_msg:
                return

            await self.db.update_one(
                {"_id": 5},
                {
                    "$set": {
                        "status_chat_id": status_msg.chat.id,
                        "status_message_id": status_msg.id,
                        "time": util.time.usec(),
                    }
                },
                upsert=True,
            )

    async def send_to_log(self, text: str, *args: Any, **kwargs: Any) -> Optional[Message]:
        if not self.bot.config.LOG_CHANNEL:
            return
        return await self.bot.client.send_message(
            int(self.bot.config.LOG_CHANNEL), text, *args, **kwargs
        )

    async def help_builder(self, chat_id: int) -> List[List[InlineKeyboardButton]]:
        """Build the help button"""
        plugins: List[InlineKeyboardButton] = []
        for plug in list(self.bot.plugins.values()):
            if plug.helpable:
                plugins.append(
                    InlineKeyboardButton(
                        await self.text(chat_id, f"{plug.name.lower()}-button"),
                        callback_data=f"help_plugin({plug.name.lower()})",
                    )
                )
        plugins.sort(key=lambda kb: kb.text)

        # Organize plugins into categories for better organization
        categories = {}
        for plugin_btn in plugins:
            # Extract category from plugin name (assuming format "category_plugin" or similar)
            category = plugin_btn.text.split()[0] if ' ' in plugin_btn.text else "General"
            if category not in categories:
                categories[category] = []
            categories[category].append(plugin_btn)

        # Create category buttons
        category_buttons = []
        for category in sorted(categories.keys()):
            category_buttons.append(
                InlineKeyboardButton(
                    f"📁 {category}",
                    callback_data=f"help_category({category})"
                )
            )

        # Organize category buttons in rows of 2
        category_rows = [category_buttons[i:i+2] for i in range(0, len(category_buttons), 2)]
        
        # Add main menu and close buttons
        category_rows.append([
            InlineKeyboardButton("🏠 Main Menu", callback_data="help_back"),
            InlineKeyboardButton("✗ Close", callback_data="help_close")
        ])

        return category_rows

    async def get_plugin_help(self, plugin_name: str, chat_id: int) -> str:
        """Get help text for a specific plugin"""
        text_lang = await self.text(chat_id, f"{plugin_name}-help", username=self.bot.user.username)
        return (
            f"Here is the help for the **{plugin_name.capitalize()}** "
            f"plugin:\n\n{text_lang}"
        )

    async def get_category_help(self, category: str, chat_id: int) -> List[List[InlineKeyboardButton]]:
        """Get plugins for a specific category"""
        plugins: List[InlineKeyboardButton] = []
        for plug in list(self.bot.plugins.values()):
            if plug.helpable:
                plugin_category = await self.text(chat_id, f"{plug.name.lower()}-category", "General")
                if plugin_category == category:
                    plugins.append(
                        InlineKeyboardButton(
                            await self.text(chat_id, f"{plug.name.lower()}-button"),
                            callback_data=f"help_plugin({plug.name.lower()})",
                        )
                    )
        
        plugins.sort(key=lambda kb: kb.text)
        
        # Organize plugins in rows of 2
        plugin_rows = [plugins[i:i+2] for i in range(0, len(plugins), 2)]
        
        # Add back button
        plugin_rows.append([
            InlineKeyboardButton("🔙 Back", callback_data="help_back"),
            InlineKeyboardButton("✗ Close", callback_data="help_close")
        ])
        
        return plugin_rows

    @listener.filters(filters.regex(r"help_(.*)"))
    async def on_callback_query(self, query: CallbackQuery) -> None:
        """Bot helper button"""
        match = query.matches[0].group(1)
        chat = query.message.chat

        if match == "back":
            keyboard = await self.help_builder(chat.id)
            try:
                await query.edit_message_text(
                    await self.text(chat.id, "help-pm", self.bot_name),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except MessageNotModified:
                pass
        elif match == "close":
            try:
                await query.message.delete()
            except MessageDeleteForbidden:
                await query.answer("I can't delete the message")
        elif match.startswith("category("):
            category = match.replace("category(", "").replace(")", "")
            keyboard = await self.get_category_help(category, chat.id)
            try:
                await query.edit_message_text(
                    f"**{category} Plugins**\n\nSelect a plugin to view its help:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except MessageNotModified:
                pass
        elif match.startswith("plugin("):
            plug = re.compile(r"plugin\((\w+)\)").match(match)
            if not plug:
                raise ValueError("Unable to find plugin name")

            text = await self.get_plugin_help(plug.group(1), chat.id)
            try:
                await query.edit_message_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🔙 Back",
                                    callback_data="help_back",
                                ),
                                InlineKeyboardButton(
                                    "✗ Close",
                                    callback_data="help_close",
                                )
                            ]
                        ]
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
            except MessageNotModified:
                pass

    async def cmd_start(self, ctx: command.Context) -> Optional[str]:
        """Bot start command"""
        chat = ctx.chat

        if chat.type == ChatType.PRIVATE:  # only send in PM's
            if ctx.input and ctx.input == "help":
                keyboard = await self.help_builder(chat.id)
                await ctx.respond(
                    await self.text(chat.id, "help-pm", self.bot_name),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return None

            if ctx.input:
                rules_re = re.compile(r"rules_(.*)")
                if rules_re.search(ctx.input):
                    plug: "Rules" = self.bot.plugins["Rules"]  # type: ignore
                    try:
                        return await plug.start_rules(ctx)
                    except (ChannelInvalid, ChannelPrivate):
                        return await self.text(chat.id, "rules-channel-invalid")

                help_re = re.compile(r"help_(.*)").match(ctx.input)
                if help_re:
                    text = await self.get_plugin_help(help_re.group(1), chat.id)
                    await ctx.respond(
                        text,
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        await self.text(chat.id, "back-button"),
                                        callback_data="help_back",
                                    ),
                                    InlineKeyboardButton(
                                        "✗ Close",
                                        callback_data="help_close",
                                    )
                                ]
                            ]
                        ),
                        disable_web_page_preview=True,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return

            permission = [
                "change_info",
                "post_messages",
                "edit_messages",
                "delete_messages",
                "restrict_members",
                "invite_users",
                "pin_messages",
                "promote_members",
                "manage_video_chats",
                "manage_chat",
            ]
            buttons = [
                [
                    InlineKeyboardButton(
                        text=await self.text(chat.id, "add-to-group-button"),
                        url=f"t.me/{self.bot.user.username}?startgroup=true&admin={'+'.join(permission)}",
                    ),
                    InlineKeyboardButton(
                        text=await self.text(chat.id, "start-help-button"),
                        url=f"t.me/{self.bot.user.username}?start=help",
                    ),
                ],
            ]
            if "Canonical" in self.bot.plugins:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=await self.text(chat.id, "status-page-button"),
                            url="https://status.userbotindo.com",
                        ),
                        InlineKeyboardButton(
                            text=await self.text(chat.id, "dashboard-button"),
                            url="https://userbotindo.com/dashboard",
                        ),
                    ]
                )
            
            # Add help button to start menu
            buttons.append([
                InlineKeyboardButton(
                    text=await self.text(chat.id, "help-button"),
                    callback_data="help_back"
                )
            ])

            await ctx.respond(
                await self.text(chat.id, "start-pm", self.bot_name),
                reply_markup=InlineKeyboardMarkup(buttons),
                disable_web_page_preview=True,
                parse_mode=ParseMode.MARKDOWN,
            )
            return None

        return await self.text(chat.id, "start-chat")

    async def cmd_help(self, ctx: command.Context) -> None:
        """Bot plugins helper"""
        chat = ctx.chat

        if chat.type != ChatType.PRIVATE:  # only send in PM's
            await ctx.respond(
                await self.text(chat.id, "help-chat"),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                text=await self.text(chat.id, "help-chat-button"),
                                url=f"t.me/{self.bot.user.username}?start=help",
                            )
                        ]
                    ]
                ),
            )
            return

        keyboard = await self.help_builder(chat.id)
        await ctx.respond(
            await self.text(chat.id, "help-pm", self.bot_name),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def cmd_quickhelp(self, ctx: command.Context) -> None:
        """Quick help command with basic instructions"""
        chat = ctx.chat
        
        quick_help_text = await self.text(chat.id, "quick-help", self.bot_name)
        
        if chat.type == ChatType.PRIVATE:
            await ctx.respond(
                quick_help_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "📚 Full Help Menu", 
                        callback_data="help_back"
                    )]
                ]),
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await ctx.respond(
                quick_help_text,
                parse_mode=ParseMode.MARKDOWN,
            )

    async def cmd_privacy(self, ctx: command.Context) -> None:
        """Bot privacy command"""
        await ctx.respond(
            await self.text(ctx.chat.id, "privacy"),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Privacy Policy", url="https://userbotindo.com/privacy")]]
            ),
        )

    async def cmd_donate(self, ctx: command.Context) -> None:
        """Bot donate command"""
        await ctx.respond(
            await self.text(ctx.chat.id, "donate"),
            disable_web_page_preview=True,
        )

    async def cmd_markdownhelp(self, ctx: command.Context) -> None:
        """Send markdown helper."""
        await ctx.respond(
            await self.text(ctx.chat.id, "markdown-helper", self.bot_name),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    @command.filters(aliases=["fillinghelp"])
    async def cmd_formathelp(self, ctx: command.Context) -> None:
        """Send markdown help."""
        await ctx.respond(
            await self.text(ctx.chat.id, "filling-format-helper", noformat=True),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
