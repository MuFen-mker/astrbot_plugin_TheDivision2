from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig 
from astrbot.api.message_components import Image
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
import asyncio
import re
import time
import json
import aiohttp
import os
from jinja2 import Template
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import sqlite3

class TheDivision2Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        if config is None:
            base = "http://127.0.0.1:8080"
            self.data_source = "ubi-go"
        else:
            base = config.get("api_base_url", "http://127.0.0.1:8080")
            ds = config.get("data_source", "ubi-go")
            # 规范化数据源值
            self.data_source = ds if ds in ("tracker", "ubi-go") else "tracker"
        self.api_base_url = base.rstrip('/')
        logger.info(f"后端基础地址: {self.api_base_url}, 数据源: {self.data_source}")
    
    #天赋查询方法
    def get_talent_data(self, talent_name: str):
        """根据天赋名称（中文或英文）从 data/data.db 中查询完整信息"""
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        if not os.path.exists(db_path):
            logger.error(f"数据库文件不存在: {db_path}")
            return None
        
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='talent'")
        if cur.fetchone():
            table_name = "talent"
        else:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='talents'")
            if cur.fetchone():
                table_name = "talents"
            else:
                logger.error("数据库中没有 talent 或 talents 表")
                conn.close()
                return None
        
        query = f"""
            SELECT name_zh, name_en, `icon path`, type, description
            FROM {table_name}
            WHERE name_zh = ? OR name_en = ?
        """
        cur.execute(query, (talent_name, talent_name))
        row = cur.fetchone()
        conn.close()
        
        if not row:
            return None
        
        return {
            "name": row["name_zh"],
            "eng_name": row["name_en"],
            "icon_url": row["icon path"],      # 数据库已存绝对路径
            "type": row["type"] or "",
            "description": row["description"] or ""
        }

    def parse_number(self, s):
        """安全地将字符串转为数字（支持逗号分隔符）"""
        if s is None or s == '':
            return 0.0
        s = str(s).replace(',', '').strip()
        try:
            return float(s)
        except:
            return 0.0

    #武器查询方法
    def get_weapon_by_name(self, weapon_name: str):
        weapon_name = weapon_name.strip()
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 精确匹配
        cur.execute("""
            SELECT name_zh, name_en, type, quality, harm, rpm, magazine_capacity,
                reload, range, head_magnification, sight, muzzle, grip, magazine,
                attributes
            FROM weapon
            WHERE name_zh = ? OR name_en = ?
        """, (weapon_name, weapon_name))
        row = cur.fetchone()
        if row:
            conn.close()
            # 处理数字和 JSON
            weapon = dict(row)
            def parse(s):
                if s is None or s == '':
                    return 0.0
                s = str(s).replace(',', '').strip()
                try:
                    return float(s)
                except:
                    return 0.0
            weapon['harm'] = parse(weapon['harm'])
            weapon['rpm'] = int(parse(weapon['rpm']))
            weapon['magazine_capacity'] = int(parse(weapon['magazine_capacity']))
            weapon['reload'] = parse(weapon['reload'])
            weapon['range'] = int(parse(weapon['range']))
            weapon['head_magnification'] = int(parse(weapon['head_magnification']))
            if weapon.get('attributes'):
                try:
                    weapon['attributes'] = json.loads(weapon['attributes'])
                except:
                    weapon['attributes'] = []
            else:
                weapon['attributes'] = []
            return weapon

        # 别名匹配
        cur.execute("SELECT name_zh, alias FROM weapon")
        rows = cur.fetchall()
        conn.close()
        for row in rows:
            alias_str = row['alias']
            if alias_str:
                aliases = [a.strip() for a in alias_str.split('\n') if a.strip()]
                if weapon_name in aliases:
                    return self.get_weapon_by_name(row['name_zh'])  # 递归调用，走精确匹配分支
        return None

    def get_weapon_attributes_map(self):
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT key, type, entry_name_zh, max_value, named FROM weapon_attributes")
        rows = cur.fetchall()
        conn.close()
        attr_map = {}
        for row in rows:
            key = row['key']
            typ = row['type']
            if key not in attr_map:
                attr_map[key] = {}
            attr_map[key][typ] = {
                'entry_name_zh': row['entry_name_zh'],
                'max_value': row['max_value'],
                'named': row['named'] == "TRUE" if row['named'] is not None else False
            }
        return attr_map

    def get_talent_by_weapon_name(self, weapon_name: str):
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT name_zh, name_en, `icon path`, type, description FROM talent WHERE type LIKE ? LIMIT 1", (f'%{weapon_name}%',))
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                'name_zh': row['name_zh'],
                'name_en': row['name_en'],
                'icon_path': row['icon path'],
                'type': row['type'],
                'description': row['description']
            }
        return None
    
    #品牌查询方法
    def get_equipment_full_data(self, name: str):
        """根据名称或别名查询装备完整信息（包括天赋描述）"""
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. 精确匹配 name_zh 或 name_en
        cur.execute("""
            SELECT 
                id, name_zh, name_en, alias, type, effect,
                set_talent,
                `enhancetalent _ chestarmor` AS enhancetalent_chestarmor,
                `enhancetalent _ backpack` AS enhancetalent_backpack
            FROM equipment_group 
            WHERE name_zh = ? OR name_en = ?
        """, (name, name))
        row = cur.fetchone()
        
        # 2. 别名匹配（如果没有精确匹配）
        if not row:
            cur.execute("SELECT id, name_zh, alias FROM equipment_group")
            all_rows = cur.fetchall()
            for r in all_rows:
                alias_str = r['alias']
                if alias_str:
                    aliases = [a.strip() for a in alias_str.split('\n') if a.strip()]
                    if name in aliases:
                        # 重新查询该行完整信息（避免重复代码）
                        cur.execute("""
                            SELECT 
                                id, name_zh, name_en, alias, type, effect,
                                set_talent,
                                `enhancetalent _ chestarmor` AS enhancetalent_chestarmor,
                                `enhancetalent _ backpack` AS enhancetalent_backpack
                            FROM equipment_group 
                            WHERE id = ?
                        """, (r['id'],))
                        row = cur.fetchone()
                        break

        if not row:
            conn.close()
            return None

        equipment = dict(row)

        # 3. 如果是装备组，补充天赋描述
        if equipment.get('type') == '装备组':
            talent_map = {}
            # 需要查询描述的三个天赋字段（标准化后的键名）
            fields = ['set_talent', 'enhancetalent_chestarmor', 'enhancetalent_backpack']
            for field in fields:
                talent_name = equipment.get(field)
                if talent_name and talent_name != '无':
                    # 去除可能的空白字符
                    talent_name_clean = talent_name.strip()
                    # 在 talent 表中查找 name_zh
                    cur.execute("SELECT description FROM talent WHERE name_zh = ?", (talent_name_clean,))
                    desc_row = cur.fetchone()
                    if desc_row:
                        talent_map[f"{field}_desc"] = desc_row['description']
                    else:
                        # 尝试模糊匹配（如果精确匹配失败）
                        cur.execute("SELECT description FROM talent WHERE name_zh LIKE ?", (f'%{talent_name_clean}%',))
                        desc_row = cur.fetchone()
                        talent_map[f"{field}_desc"] = desc_row['description'] if desc_row else '暂无描述'
                else:
                    talent_map[f"{field}_desc"] = None
            equipment.update(talent_map)

        conn.close()
        return equipment

    @filter.command("数据查询")
    async def on_query(self, event: AstrMessageEvent, username: str):
        if self.data_source == "tracker":
            platform = "ubi"
            def build_tracker_url(platform: str, username: str) -> str:
                return f"https://tracker.gg/division-2/profile/{platform}/{username}/overview"
            url = build_tracker_url(platform, username)
            
            async with AsyncSession() as session:
                response = await session.get(url, impersonate="edge101", timeout=120)
            
            if response.status_code != 200:
                body = response.text[:500] if response.text else "无响应体"
                logger.error(f"请求异常：{response.status_code}\n"
                            f"URL:{url}\n"
                            f"响应体：\n{body}"
                            )
                yield event.plain_result(
                    f"网络错误，请稍后重试！"
                )
                return               
            soup = BeautifulSoup(response.text, 'html.parser')
            #===================
            #头像
            avatarimg = soup.find("img", class_="user-avatar__image")
            avatar_url = avatarimg.get('src') if avatarimg else None
            logger.info(f"avatar_url type: {type(avatar_url)}, value: {avatar_url}")
            #等级
            level_span = soup.find('span', title='Player Level')
            Level = level_span.find_next_sibling('span').find('span', class_='value').get_text(strip=True) if level_span else None
            #暗区等级
            dz_span = soup.find('span', title='DZ Level')
            DZLevel = dz_span.find_next_sibling('span').find('span', class_='value').get_text(strip=True) if dz_span else None
            #游戏时长
            gametime = int(re.search(r'(\d+(?:,\d+)*)', soup.find('h2', string='Lifetime Overview').find_parent('div', class_='details').find('div', class_='title-stats').find('span', class_='playtime').get_text(strip=True)).group(1).replace(',', ''))
            #功勋
            CurrComm = soup.find('span', class_='name', title='Curr Comm. Score').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            CurrComm = int(CurrComm.replace(',', '')) if CurrComm else None
            #物品拾取数量
            ItemsLooted = soup.find('span', class_='name', title='Items Looted').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            ItemsLooted = int(ItemsLooted.replace(',', '')) if ItemsLooted else None
            #玩家击杀
            PvpKills = soup.find('span', class_='name', title='PvP Kills').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            PvpKills = int(PvpKills.replace(',', '')) if PvpKills else None
            #NPC击杀
            NpcKills = soup.find('span', class_='name', title='NPC Kills').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            NpcKills = int(NpcKills.replace(',', '')) if NpcKills else None
            #技能击杀
            SkillKills = soup.find('span', class_='name', title='Skill Kills').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            SkillKills = int(SkillKills.replace(',', '')) if SkillKills else None
            #爆头数量
            Headshots = soup.find('span', class_='name', title='Headshots').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            Headshots = int(Headshots.replace(',', '')) if Headshots else None
            #E点数
            ECreditBalance = soup.find('span', class_='name', title='E-Credit Balance').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            ECreditBalance = int(ECreditBalance.replace(',', '')) if ECreditBalance else None
            #PVE经验
            PveXP = soup.find('span', class_='name', title='PvE XP').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            PveXP = int(PveXP.replace(',', '')) if PveXP else None
            #具名击杀
            NamedKills = int(soup.find('h2', string='PvE').find_parent('div', class_='card').find('span', class_='name', title='Named Kills').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True).replace(',', ''))
            #鬣狗击杀
            HyenaKills = soup.find('span', class_='name', title='Hyena Kills').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            HyenaKills = int(HyenaKills.replace(',', '')) if HyenaKills else None
            #流亡者击杀
            OutCastsKills = soup.find('span', class_='name', title='OutCasts Kills').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            OutCastsKills = int(OutCastsKills.replace(',', '')) if OutCastsKills else None
            #真实之子击杀
            TrueSonsKills = soup.find('span', class_='name', title='TrueSons Kills').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            TrueSonsKills = int(TrueSonsKills.replace(',', '')) if TrueSonsKills else None
            #黯牙击杀
            BlackTuskKills = int(soup.find('h2', string='PvE').find_parent('div', class_='card').find('span', class_='name', title='BlackTusk Kills').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True).replace(',', ''))
            #暗区时长
            h2 = soup.find('h2', string='Dark Zone')
            if h2:
                playtime_span = h2.find_next('span', class_='playtime')
                if playtime_span:
                    text = playtime_span.get_text(strip=True)
                    match = re.search(r'(\d+)', text)
                    dzPlaytime = int(match.group(1)) if match else None
            #暗区经验
            DzXp = soup.find('span', class_='name', title='DZ XP').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            DzXp = int(DzXp.replace(',', '')) if DzXp else None
            #冲突战等级
            conflict_span = soup.find('span', class_='name', title='Conflict Rank').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            conflict_span = int(conflict_span.replace(',', '')) if conflict_span else None
            #叛变击杀
            RoguesKilled = soup.find('span', class_='name', title='Rogues Killed').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            RoguesKilled = int(RoguesKilled.replace(',', '')) if RoguesKilled else None
            #叛变时长
            RogueTimePlayed = soup.find('span', class_='name', title='Rogue Time Played').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            #最长叛变时间
            RogueLongestTimePlayed = soup.find('span', class_='name', title='Rogue Longest Time Played').find_parent('div', class_='numbers').find('span', class_='value').get_text(strip=True)
            #流血击杀
            BleedingKills = soup.find('td', string='Bleeding Kills').find_next_sibling('td').get_text(strip=True)
            BleedingKills = int(BleedingKills.replace(',', '')) if BleedingKills else None
            #燃烧击杀
            BurningKills = soup.find('td', string='Burning Kills').find_next_sibling('td').get_text(strip=True)
            BurningKills = int(BurningKills.replace(',', '')) if BurningKills else None
            #爆头击杀
            HeadshotKills = soup.find('td', string='Headshot Kills').find_next_sibling('td').get_text(strip=True)
            HeadshotKills = int(HeadshotKills.replace(',', '')) if HeadshotKills else None
            #冲锋枪击杀
            SMGKills = soup.find('td', string='SMG Kills').find_next_sibling('td').get_text(strip=True)
            SMGKills = int(SMGKills.replace(',', '')) if SMGKills else None
            #霰弹枪击杀
            ShotgunKills = soup.find('td', string='Shotgun Kills').find_next_sibling('td').get_text(strip=True)
            ShotgunKills = int(ShotgunKills.replace(',', '')) if ShotgunKills else None
            #步枪击杀
            RifleKills = soup.find('td', string='Rifle Kills').find_next_sibling('td').get_text(strip=True)
            RifleKills = int(RifleKills.replace(',', '')) if RifleKills else None
            #手枪击杀
            PistolKills = soup.find('td', string='Pistol Kills').find_next_sibling('td').get_text(strip=True)
            PistolKills = int(PistolKills.replace(',', '')) if PistolKills else None
            #总命中数
            hit = "无"
            #身体命中数
            bodyHit = "无"
            #头部命中率
            HeadHitRate = "无"
            #爆头和身体命中比
            HeadshotToBodyshotRatio = "无"
            #每小时击杀数
            KillRatePerHour = "无"
            #每小时爆头命中数
            HourlyHeadcountHits = "无"
            #每小时身体命中数
            HourlyBodyHits = "无"
            #当日击杀数
            DailyKills = "无"
            #当日爆头数
            DailyHeadcount = "无"
            logger.info(f"数据字典已生成，数据来源：tracker.gg")

        elif self.data_source == "ubi-go":
            # 1. 获取玩家 UID
            profile_url = f"{self.api_base_url}/profile?username={username}&platform=uplay"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(profile_url, timeout=10) as resp:
                        if resp.status != 200:
                            yield event.plain_result(f"获取UID失败，状态码：{resp.status}")
                            return
                        profile_data = await resp.json()
                        uid = profile_data.get("data", {}).get("UserId")  # 根据实际返回字段调整
                        if not uid:
                            yield event.plain_result("未找到玩家UID")
                            return
            except Exception as e:
                logger.error(f"请求UID异常：{e}")
                yield event.plain_result("网络错误，请稍后重试")
                return

            # 2. 获取玩家统计数据
            stats_url = f"{self.api_base_url}/stats?gameId=60859c37-949d-49e2-8fc8-6d8dc40f1a9e&platform=uplay&uids={uid}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(stats_url, timeout=10) as resp:
                        if resp.status != 200:
                            yield event.plain_result(f"获取统计数据失败，状态码：{resp.status}")
                            return
                        stats_data = await resp.json()
            except Exception as e:
                logger.error(f"请求统计异常：{e}")
                yield event.plain_result("网络错误，请稍后重试")
                return
            
            #时间格式化函数
            def format_duration(seconds):
                try:
                    seconds = int(seconds)
                except (ValueError, TypeError):
                    return "0秒"
                if seconds <= 0:
                    return "0秒"
                h = seconds // 3600
                m = (seconds % 3600) // 60
                s = seconds % 60
                parts = []
                if h:
                    parts.append(f"{h}小时")
                if m:
                    parts.append(f"{m}分钟")
                if s:
                    parts.append(f"{s}秒")
                return "".join(parts) if parts else "0秒"
            
            #武器击杀求和
            def get_weapon_total(stats_obj, family):
                total = 0
                prefix = f"weaponFactionKills.weaponFamily.{family}.npcFaction."
                for key, value in stats_obj.items():
                    if key.startswith(prefix):
                        total += int(value.get("value", 0))
                return total
            
            player_data = stats_data["data"][0]["stats"]
            #===================
            #头像
            avatar_url = f"https://ubisoft-avatars.akamaized.net/{uid}/default_146_146.png"
            logger.info(f"avatar_url type: {type(avatar_url)}, value: {avatar_url}")
            #等级
            Level = player_data.get("LatestLevel.rankType.NormalXP", {}).get("value", "0")
            #暗区等级
            DZLevel = player_data.get("LatestLevel.rankType.DarkZoneXP", {}).get("value", "0")
            #游戏时长
            playtime_seconds = int(player_data.get("Playtime", {}).get("value", "0"))
            gametime = round(playtime_seconds / 3600, 1) if playtime_seconds else 0
            #功勋
            CurrComm = player_data.get("LatestCommendationScore", {}).get("value", "0")
            #物品拾取数量
            ItemsLooted = player_data.get("SumItemsLooted", {}).get("value", "0")
            #玩家击杀
            PvpKills = player_data.get("SumPvpKills", {}).get("value", "0")
            #NPC击杀
            NpcKills = int(player_data.get("SumNpcKills", {}).get("value", "0"))
            #技能击杀
            SkillKills = player_data.get("SumSkillKills", {}).get("value", "0")
            #爆头数量
            Headshots = int(player_data.get("SumHeadShots", {}).get("value", "0"))
            #E点数
            ECreditBalance = int(player_data.get("LatestWalletBalanceSplit.currencyName.E-Credits", {}).get("value", "0"))
            #PVE经验
            PveXP = player_data.get("TotalXpOw", {}).get("value", "0")
            #具名击杀
            NamedKills = player_data.get("specialRoleKills.npcSpecialRole.named", {}).get("value", "0")
            #鬣狗击杀
            HyenaKills = player_data.get("factionDarkZoneKills.npcFaction.Blackbloc", {}).get("value", "0")
            #流亡者击杀
            OutCastsKills = player_data.get("factionDarkZoneKills.npcFaction.Cultists", {}).get("value", "0")
            #真实之子击杀
            TrueSonsKills = player_data.get("factionDarkZoneKills.npcFaction.Militia", {}).get("value", "0")
            #黯牙击杀
            BlackTuskKills = player_data.get("factionKills.npcFaction.Endgame", {}).get("value", "0")
            #暗区时长
            dzplaytime_seconds = int(player_data.get("TotalPlaytimeDarkzone", {}).get("value", "0"))
            dzPlaytime = round(dzplaytime_seconds / 3600, 1) if dzplaytime_seconds else 0
            #暗区经验
            DzXp = player_data.get("TotalXpDz", {}).get("value", "0")
            #冲突战等级
            conflict_span = player_data.get("LatestLevel.rankType.OrganizedPvpXP", {}).get("value", "0")
            #叛变击杀
            RoguesKilled = player_data.get("numberOfRoguePlayerKills", {}).get("value", "0")
            #叛变时长
            RogueTimePlayed_seconds = player_data.get("TotalPlaytimeRogue", {}).get("value", "0")
            RogueTimePlayed = format_duration(RogueTimePlayed_seconds)
            #最长叛变时间
            RogueLongestTimePlayed_seconds = player_data.get("MaxRogueTime", {}).get("value", "0")
            RogueLongestTimePlayed = format_duration(RogueLongestTimePlayed_seconds)
            #流血击杀
            BleedingKills = player_data.get("bleedingKills", {}).get("value", "0")
            #燃烧击杀
            BurningKills = player_data.get("burningKills", {}).get("value", "0")
            #爆头击杀
            HeadshotKills = player_data.get("headshotKills", {}).get("value", "0")
            #冲锋枪击杀
            SMGKills = get_weapon_total(player_data, "SubMachinegun")
            #霰弹枪击杀
            ShotgunKills = get_weapon_total(player_data, "Shotgun")
            #步枪击杀
            RifleKills = player_data.get("weaponFamilyKills.weaponFamily.MountedWeapon", {}).get("value", "0")
            #手枪击杀
            PistolKills = player_data.get("weaponFamilyKills.weaponFamily.Pistol", {}).get("value", "0")
            #总命中数
            hit = int(player_data.get("SumHits", {}).get("value", "0"))
            #身体命中数
            bodyHit = hit - Headshots
            #头部命中率
            HeadHitRate = f"{Headshots/hit*100:.1f}%"
            #爆头和身体命中比
            HeadshotToBodyshotRatio = f"{((lambda r: f'{int(r)}' if r.is_integer() else f'{r:.1f}')(bodyHit/Headshots))}次身体:1次头部"
            #每小时击杀数
            KillRatePerHour = int(NpcKills/gametime)
            #每小时爆头命中数
            HourlyHeadcountHits = int(Headshots/gametime)
            #每小时身体命中数
            HourlyBodyHits = int(bodyHit/gametime)
            #当日击杀数
            DailyKills = player_data.get("npckillsperiodic", {}).get("value", "0")
            #当日爆头数
            DailyHeadcount = player_data.get("DailySumHeadShots", {}).get("value", "0")
            logger.info(f"数据字典已生成，数据来源：UBI-GO URL:{self.api_base_url}")

        #整理字典
        player_info_data = {
            "playername": username,
            "avatarimg": avatar_url,
            "Level": Level,
            "DZLevel": DZLevel,
            "gametime": gametime,
            "CurrComm":CurrComm,
            "ItemsLooted":ItemsLooted,
            "PvpKills":PvpKills,
            "NpcKills":NpcKills,
            "SkillKills":SkillKills,
            "Headshots":Headshots,
            "ECreditBalance":ECreditBalance,
            "PveXP":PveXP,
            "NamedKills":NamedKills,
            "HyenaKills":HyenaKills,
            "OutCastsKills":OutCastsKills,
            "TrueSonsKills":TrueSonsKills,
            "BlackTuskKills":BlackTuskKills,
            "dzPlaytime":dzPlaytime,
            "DzXp":DzXp,
            "conflict_span":conflict_span,
            "RoguesKilled":RoguesKilled,
            "RogueTimePlayed":RogueTimePlayed,
            "RogueLongestTimePlayed":RogueLongestTimePlayed,
            "BleedingKills":BleedingKills,
            "BurningKills":BurningKills,
            "HeadshotKills":HeadshotKills,
            "SMGKills":SMGKills,
            "ShotgunKills":ShotgunKills,
            "RifleKills":RifleKills,
            "PistolKills":PistolKills,
            "hit":hit,
            "bodyHit":bodyHit,
            "HeadHitRate":HeadHitRate,
            "HeadshotToBodyshotRatio":HeadshotToBodyshotRatio,
            "KillRatePerHour":KillRatePerHour,
            "HourlyHeadcountHits":HourlyHeadcountHits,
            "HourlyBodyHits":HourlyBodyHits,
            "DailyKills":DailyKills,
            "DailyHeadcount":DailyHeadcount
        }

        #导入渲染模板
        template_path = os.path.join(os.path.dirname(__file__), "templates", "player_info.html")
        with open(template_path, "r", encoding="utf-8") as f:
            template_str = f.read()
        template = Template(template_str)
        html = template.render(player_info_data) 
        
        options = {
            "type": "png", 
            "full_page":True,
            "scale":"css",
            "omit_background": True
        }
        imgurl = await self.html_render(html, {}, options=options)
        yield event.image_result(imgurl)  

    @filter.command("周商")
    async def weekly_vendor(self, event: AstrMessageEvent):
        # 缓存文件路径
        cache_dir = Path(get_astrbot_data_path()) / "plugin_data" / self.name / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "weekly_vendor.jpg"
        cache_ttl = 3600

        # 检查缓存是否有效
        if cache_file.exists():
            mtime = cache_file.stat().st_mtime
            if time.time() - mtime < cache_ttl:
                logger.info("使用缓存的周商图片")
                yield event.image_result(str(cache_file))
                return

        # 1. 获取原始 JSON 数据
        url = "https://raw.githubusercontent.com/MuFen-mker/astrbot_plugin_TheDivision2_DataAPI/refs/heads/main/all_vendors.json"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        yield event.plain_result(f"获取数据失败，状态码：{resp.status}")
                        return
                    data = await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"请求异常：{e}")
            yield event.plain_result("网络错误，请稍后重试")
            return

        # 2. 数据库连接及辅助函数（内嵌）
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # ---------- 缓存所有属性最大值 ----------
        # 武器属性缓存 (key, type) -> max_value (数值)
        weapon_attr_max = {}
        cur.execute("SELECT key, type, max_value FROM weapon_attributes")
        for row in cur.fetchall():
            key, typ, val = row['key'], row['type'], row['max_value']
            num = 0.0
            if val:
                try:
                    num = float(val.replace('%', '').strip())
                except:
                    num = 0.0
            weapon_attr_max[(key, typ)] = num

        # 护甲属性缓存 (key, type) -> max_value
        gear_attr_max = {}
        cur.execute("SELECT key, type, max_value FROM gear_attributes")
        for row in cur.fetchall():
            key, typ, val = row['key'], row['type'], row['max_value']
            num = 0.0
            if val:
                try:
                    num = float(val.replace('%', '').strip())
                except:
                    num = 0.0
            gear_attr_max[(key, typ)] = num

        # 模组属性缓存 (attributes_en) -> max_value
        mod_attr_max = {}
        cur.execute("SELECT attributes_en, max_value FROM mods_attributes")
        for row in cur.fetchall():
            key, val = row['attributes_en'], row['max_value']
            num = 0.0
            if val:
                try:
                    num = float(val.replace('%', '').strip())
                except:
                    num = 0.0
            mod_attr_max[key] = num

        # ---------- 翻译辅助函数（使用数据库查询，无硬编码）----------
        def get_translation(en_name):
            cur.execute("SELECT name_zh FROM translations WHERE name_en = ?", (en_name,))
            row = cur.fetchone()
            return row['name_zh'] if row else en_name

        def get_weapon_name_zh(en_name):
            cur.execute("SELECT name_zh FROM weapon WHERE name_en = ?", (en_name,))
            row = cur.fetchone()
            return row['name_zh'] if row else en_name

        def get_gear_name_zh(en_name):
            cur.execute("SELECT name_zh FROM gear WHERE name_en = ?", (en_name,))
            row = cur.fetchone()
            return row['name_zh'] if row else en_name

        def get_brand_name_zh(en_name):
            cur.execute("SELECT name_zh FROM equipment_group WHERE name_en = ?", (en_name,))
            row = cur.fetchone()
            return row['name_zh'] if row else en_name

        def get_talent_name_zh(en_name):
            cur.execute("SELECT name_zh FROM talent WHERE name_en = ?", (en_name,))
            row = cur.fetchone()
            return row['name_zh'] if row else en_name

        def get_mod_name_zh(en_name):
            cur.execute("SELECT name_zh FROM mods WHERE name_en = ?", (en_name,))
            row = cur.fetchone()
            return row['name_zh'] if row else en_name

        # 属性中文名查询（武器和护甲都使用 gear_attributes 的 entry_name_zh）
        def get_attr_zh(attr_key):
            cur.execute("SELECT entry_name_zh FROM gear_attributes WHERE key = ?", (attr_key,))
            row = cur.fetchone()
            return row['entry_name_zh'] if row else attr_key

        def get_mod_attr_zh(attr_en):
            cur.execute("SELECT attributes_zh FROM mods_attributes WHERE attributes_en = ?", (attr_en,))
            row = cur.fetchone()
            return row['attributes_zh'] if row else attr_en

        # ---------- 递归翻译函数（核心）----------
        def translate_value(obj, context=None, key=None, item_type=None):
            if isinstance(obj, dict):
                # 检测物品类型
                if 'gears' in obj:
                    item_type = 'gear'
                elif 'weapons' in obj:
                    item_type = 'weapon'
                elif 'mods' in obj:
                    item_type = 'mod'

                new_dict = {}
                for k, v in obj.items():
                    # ---------- 武器属性 ----------
                    if item_type == 'weapon' and k == 'attributes' and isinstance(v, list):
                        # 武器属性数组：最后一条是次要属性
                        attr_list = v
                        total = len(attr_list)
                        translated = []
                        for idx, attr_key in enumerate(attr_list):
                            attr_type = 'secondary' if idx == total - 1 else 'core'
                            # 查询最大值
                            max_num = weapon_attr_max.get((attr_key, attr_type), 0.0)
                            attr_zh = get_attr_zh(attr_key)
                            translated.append(attr_zh)
                            # 存储最大值（供后续进度条使用）
                            new_dict[f'attr_{idx}_max'] = max_num
                        new_dict[k] = translated
                    # ---------- 护甲属性 ----------
                    elif item_type == 'gear' and k in ('Core', 'attribute1', 'attribute2', 'attribute3', 'attributes'):
                        if k == 'Core':
                            attr_type = 'gear_core'
                        else:
                            attr_type = 'gear_secondary'
                        max_num = gear_attr_max.get((v, attr_type), 0.0)
                        attr_zh = get_attr_zh(v)
                        new_dict[k] = attr_zh
                        new_dict[f'{k}_max'] = max_num
                    # ---------- 模组属性 ----------
                    elif item_type == 'mod' and k == 'attributes':
                        max_num = mod_attr_max.get(v, 0.0)
                        attr_zh = get_mod_attr_zh(v)
                        new_dict[k] = attr_zh
                        new_dict[f'{k}_max'] = max_num
                    # ---------- 名称翻译 ----------
                    elif k == 'name':
                        if context == 'gears':
                            new_dict[k] = get_gear_name_zh(v)
                        elif context == 'weapons':
                            new_dict[k] = get_weapon_name_zh(v)
                        elif context == 'mods':
                            new_dict[k] = get_mod_name_zh(v)
                        else:
                            new_dict[k] = v
                    # ---------- 品牌 ----------
                    elif k == 'brand':
                        new_dict[k] = get_brand_name_zh(v)
                    # ---------- 类型（部位/技能）----------
                    elif k == 'type':
                        if context == 'mods':
                            new_dict[k] = get_translation(v)  # 技能名
                        else:
                            new_dict[k] = get_translation(v)  # 部位名
                    # ---------- 天赋 ----------
                    elif k == 'talent':
                        new_dict[k] = get_talent_name_zh(v)
                    # ---------- 递归处理嵌套 ----------
                    else:
                        sub_context = context
                        if k == 'gears':
                            sub_context = 'gears'
                        elif k == 'weapons':
                            sub_context = 'weapons'
                        elif k == 'mods':
                            sub_context = 'mods'
                        new_dict[k] = translate_value(v, sub_context, k, item_type)
                return new_dict
            elif isinstance(obj, list):
                return [translate_value(item, context, key, item_type) for item in obj]
            else:
                return obj

        # ---------- 翻译商人名称并处理数据 ----------
        translated_data = {}
        for merchant_en, vendor_data in data.items():
            merchant_zh = get_translation(merchant_en)
            translated_data[merchant_zh] = translate_value(vendor_data)

        conn.close()

        # ---------- 后续处理（提取数字、颜色、进度条等，复用原有逻辑）----------
        # 注意：这里去掉了所有硬编码的 max 映射和特殊规则，
        # 因为每个属性都已经有 _max 字段，可以直接使用。

        def extract_number(s):
            match = re.search(r'([\d,]+(?:\.\d+)?)', s)
            if match:
                return float(match.group(1).replace(',', ''))
            return 0.0

        def get_attribute_color(attr_str):
            if any(kw in attr_str for kw in ['装甲回复', '危害防护', '爆炸抗性', '生命值']):
                return '#289eff'
            elif any(kw in attr_str for kw in ['爆头伤害', '暴击机率', '暴击伤害', '生命值伤害', '对装甲的伤害', '对离开掩体目标的伤害', '武器伤害', '手枪伤害', '武器控制力', '准确度', '稳定度', '弹药容量']):
                return '#ff4242'
            elif any(kw in attr_str for kw in ['技能加速', '技能伤害', '修复技能', '技能持续时间']):
                return '#f6ff42'
            else:
                return '#fba000'

        # 预处理数据：添加数值、颜色等（使用从数据库获取的 _max 字段）
        for vendor_data in translated_data.values():
            # 护甲
            for gear in vendor_data.get('gears', []):
                gear['Core_value'] = extract_number(gear.get('Core', ''))
                if '装甲' in gear.get('Core', ''):
                    gear['gradient_color'] = '#289eff'
                    gear['Core_color'] = '#289eff'
                elif '武器伤害' in gear.get('Core', ''):
                    gear['gradient_color'] = '#ff2e2e'
                    gear['Core_color'] = '#ff2e2e'
                elif '技能分阶' in gear.get('Core', ''):
                    gear['gradient_color'] = '#f18600'
                    gear['Core_color'] = '#f18600'
                else:
                    gear['gradient_color'] = '#289eff'
                    gear['Core_color'] = '#289eff'

                for attr in ['attribute1', 'attribute2', 'attribute3']:
                    if gear.get(attr) and gear[attr] != '-':
                        gear[f'{attr}_value'] = extract_number(gear[attr])
                        gear[f'{attr}_color'] = get_attribute_color(gear[attr])

            # 武器
            for weapon in vendor_data.get('weapons', []):
                for i in range(3):  # attribute1, attribute2, attribute3
                    attr_key = f'attribute{i+1}'
                    if weapon.get(attr_key) and weapon[attr_key] != '-':
                        weapon[f'{attr_key}_value'] = extract_number(weapon[attr_key])

            # 模组
            for mod in vendor_data.get('mods', []):
                if mod.get('type') == '护甲模组':
                    mod['attributes_value'] = extract_number(mod.get('attributes', ''))
                    mod['attributes_color'] = get_attribute_color(mod.get('attributes', ''))
                    if '攻击协定' in mod.get('name', ''):
                        mod['gradient_color'] = '#770000'
                        mod['icon_prefix'] = '攻击协定'
                    elif '防御协定' in mod.get('name', ''):
                        mod['gradient_color'] = '#003f73'
                        mod['icon_prefix'] = '防御协定'
                    elif '性能协定' in mod.get('name', ''):
                        mod['gradient_color'] = '#ab5f00'
                        mod['icon_prefix'] = '性能协定'
                    else:
                        mod['gradient_color'] = '#fba000'
                        mod['icon_prefix'] = '护甲模组'

        # 加载模板并渲染
        template_path = os.path.join(os.path.dirname(__file__), "templates", "weekly_report.html")
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                template_str = f.read()
        except Exception as e:
            logger.error(f"读取模板失败: {e}")
            yield event.plain_result("模板加载失败")
            return

        template = Template(template_str)
        html = template.render(data=translated_data, vendor_name_map={})  # 商人名称已翻译

        # 生成图片并缓存
        options = {
            "type": "jpeg",
            "quality": 75,
            "device_scale_factor": 1,
        }
        try:
            img_url = await self.html_render(html, {}, options=options)
        except Exception as e:
            logger.error(f"图片渲染失败: {e}")
            yield event.plain_result("生成图片失败，请稍后重试")
            return

        # 下载图片到缓存文件
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(img_url) as resp:
                    if resp.status == 200:
                        with open(cache_file, "wb") as f:
                            f.write(await resp.read())
                        logger.info(f"图片已缓存到 {cache_file}")
                    else:
                        logger.error(f"下载图片失败，状态码：{resp.status}")
                        yield event.plain_result("图片下载失败，请稍后重试")
                        return
        except Exception as e:
            logger.error(f"下载图片异常: {e}")
            yield event.plain_result("图片下载失败，请稍后重试")
            return

        # 发送本地缓存图片
        yield event.image_result(str(cache_file))

    @filter.command("天赋")
    async def talent_query(self, event: AstrMessageEvent, talent_name: str = None):
        if not talent_name:
            yield event.plain_result("请提供天赋名称，例如：/天赋 反复")
            return
        talent = self.get_talent_data(talent_name.strip())
        if not talent:
            yield event.plain_result(f"未找到名为「{talent_name}」的天赋")
            return

        # 手动加载模板文件（与 on_query 中的方式一致）
        template_path = os.path.join(os.path.dirname(__file__), "templates", "talent_card.html")
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                template_str = f.read()
        except FileNotFoundError:
            yield event.plain_result("模板文件 talent_card.html 未找到")
            return

        template = Template(template_str)
        html = template.render(talent=talent)

        # 生成图片
        options = {
            "type": "png", 
            "full_page":True,
            "scale":"css",
            "omit_background": True
        }
        img_url = await self.html_render(html, {}, options=options)
        yield event.image_result(img_url)

    @filter.command("武器")
    async def weapon_query(self, event: AstrMessageEvent):
        # 获取原始消息字符串（AstrBot 已自动去除 @ 提及）
        text = event.message_str.strip()
        # 正则匹配：/武器 后面的所有内容（支持 /武器 和 武器）
        match = re.search(r'^(?:/)?武器\s+(.+)$', text)
        if not match:
            yield event.plain_result("请提供武器名称，例如：/武器 战术 M1911")
            return
        weapon_name = match.group(1).strip()
        if not weapon_name:
            yield event.plain_result("请提供武器名称，例如：/武器 战术 M1911")
            return

        # 查询武器（必须调用）
        weapon = self.get_weapon_by_name(weapon_name)
        if not weapon:
            yield event.plain_result(f"未找到名为「{weapon_name}」的武器")
            return

        # 获取属性映射表
        attr_map = self.get_weapon_attributes_map()

        # 构建武器属性列表（根据位置确定类型）
        attributes_list = []
        total = len(weapon['attributes'])
        for idx, attr_key in enumerate(weapon['attributes']):
            is_last = (idx == total - 1)
            attr_type = 'secondary' if is_last else 'core'
            # 获取属性信息（优先根据类型获取，若无则降级）
            attr_info = attr_map.get(attr_key, {}).get(attr_type, {})
            if not attr_info:
                for any_info in attr_map.get(attr_key, {}).values():
                    attr_info = any_info
                    break
            display_name = attr_info.get('entry_name_zh', attr_key)
            max_value = attr_info.get('max_value', '-')
            is_named = attr_info.get('named', False)

            if attr_key == '随机词条':
                display_value = '-'
                prototype_value = '-'
            else:
                display_value = max_value if max_value else '-'
                if weapon['quality'] != '奇特' and display_value != '-':
                    try:
                        if '%' in display_value:
                            num = float(display_value.strip('%'))
                            prototype_num = round(num * 1.5, 1)
                            prototype_value = f"{prototype_num}%"
                        else:
                            num = float(display_value)
                            prototype_value = round(num * 1.5, 1)
                    except:
                        prototype_value = '-'
                else:
                    prototype_value = '-'
            attributes_list.append({
                'name': display_name,
                'value': display_value,
                'prototype': prototype_value,
                'special': is_named,
                'type': attr_type
            })

        # 特殊爆头金色标记（根据属性中是否有名为“爆头伤害”的特殊词条）
        special_headshot = any(attr['name'] == '爆头伤害' and attr.get('special') for attr in attributes_list)

        talent = self.get_talent_by_weapon_name(weapon['name_zh'])

        # 准备模板数据
        template_data = {
            'weapon': {
                'name_zh': weapon['name_zh'],
                'name_en': weapon['name_en'],
                'type': weapon['type'],
                'quality': weapon['quality'],
                'harm': weapon['harm'],
                'rpm': weapon['rpm'],
                'magazine_capacity': weapon['magazine_capacity'],
                'reload': weapon['reload'],
                'range': weapon['range'],
                'head_magnification': weapon['head_magnification'],
                'special_headshot': special_headshot,
                'sight': weapon['sight'],
                'muzzle': weapon['muzzle'],
                'grip': weapon['grip'],
                'magazine': weapon['magazine'],
                'attributes': attributes_list,
                'talent': talent
            },
            'attr_map': attr_map
        }

        # 渲染模板
        template_path = os.path.join(os.path.dirname(__file__), "templates", "weapon_card.html")
        if not os.path.exists(template_path):
            yield event.plain_result("武器卡片模板文件未找到")
            return
        with open(template_path, "r", encoding="utf-8") as f:
            template_str = f.read()
        template = Template(template_str)
        html = template.render(**template_data)

        options = {
            "type": "png",
            "full_page": True,
            "scale": "css",
            "omit_background": True
        }
        try:
            img_url = await self.html_render(html, {}, options=options)
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"武器卡片渲染失败: {e}")
            yield event.plain_result("生成图片失败，请稍后重试")

    @filter.command("套装")
    async def equipment_query(self, event: AstrMessageEvent, name: str = None):
        if not name:
            yield event.plain_result("请提供装备品牌或装备组名称，例如：/套装 核心力量")
            return

        equipment = self.get_equipment_full_data(name.strip())
        if not equipment:
            yield event.plain_result(f"未找到名为「{name}」的装备")
            return

        # 渲染模板
        template_path = os.path.join(os.path.dirname(__file__), "templates", "equipment_card.html")
        if not os.path.exists(template_path):
            yield event.plain_result("装备卡片模板文件未找到")
            return
        with open(template_path, "r", encoding="utf-8") as f:
            template_str = f.read()
        template = Template(template_str)
        html = template.render(group=equipment)

        # 生成图片
        options = {
            "type": "png",
            "full_page": True,
            "scale": "css",
            "omit_background": True
        }
        try:
            img_url = await self.html_render(html, {}, options=options)
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"装备卡片渲染失败: {e}")
            yield event.plain_result("生成图片失败，请稍后重试")
    
    @filter.command("装备")
    async def gear_query(self, event: AstrMessageEvent, name: str = None):
        if not name:
            yield event.plain_result("请提供装备名称，例如：/装备 魔鬼回报")
            return
        gear_name = name.strip()

        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # ------------------- 1. 查询装备（支持别名） -------------------
        # 精确匹配
        cur.execute("SELECT * FROM gear WHERE name_zh = ? OR name_en = ?", (gear_name, gear_name))
        row = cur.fetchone()
        if not row:
            # 别名匹配：遍历 alias 字段（换行分隔）
            cur.execute("SELECT name_zh, alias FROM gear")
            rows = cur.fetchall()
            for r in rows:
                alias_str = r['alias']
                if alias_str:
                    aliases = [a.strip() for a in alias_str.split('\n') if a.strip()]
                    if gear_name in aliases:
                        cur.execute("SELECT * FROM gear WHERE name_zh = ?", (r['name_zh'],))
                        row = cur.fetchone()
                        break
        if not row:
            conn.close()
            yield event.plain_result(f"未找到名为「{gear_name}」的装备")
            return
        gear = dict(row)

        # 解析 attributes JSON
        if gear.get('attributes'):
            try:
                gear['attributes'] = json.loads(gear['attributes'])
            except:
                gear['attributes'] = []
        else:
            gear['attributes'] = []

        # ------------------- 2. 查询 gear_attributes 映射 -------------------
        cur.execute("SELECT key, type, icon, entry_name_zh, max_value, named FROM gear_attributes")
        attr_rows = cur.fetchall()
        attr_map = {}
        for ar in attr_rows:
            attr_map[ar['key']] = {
                'type': ar['type'],
                'icon': ar['icon'],
                'entry_name_zh': ar['entry_name_zh'],
                'max_value': ar['max_value'],
                'named': ar['named']   # "TRUE"/"FALSE"
            }

        # ------------------- 3. 查询天赋（如果 gear.talent 为 "TRUE"） -------------------
        talent_data = None
        if gear.get('talent') == 'TRUE':
            # 模糊匹配 talent.type 字段，包含装备名称
            cur.execute("SELECT name_zh, name_en, `icon path`, description FROM talent WHERE type LIKE ? LIMIT 1", (f'%{gear["name_zh"]}%',))
            t_row = cur.fetchone()
            if t_row:
                talent_data = {
                    'name_zh': t_row['name_zh'],
                    'name_en': t_row['name_en'],
                    'icon_path': t_row['icon path'],
                    'description': t_row['description']
                }
        conn.close()

        # ------------------- 4. 构建属性列表（供模板使用） -------------------
        attributes_list = []
        for attr_key in gear['attributes']:
            info = attr_map.get(attr_key, {})
            # 重要：保留原始的 max_value 字符串（如 "0.15"）
            max_value_raw = info.get('max_value', '0')
            attributes_list.append({
                'key': attr_key,
                'name': info.get('entry_name_zh', attr_key),
                'icon': info.get('icon', '武器属性.png'),
                'max_value': max_value_raw,   # 直接使用原始字符串
                'named': info.get('named', 'FALSE') == 'TRUE'
            })

        # ------------------- 5. 品质映射到 CSS 类 -------------------
        quality_to_class = {
            '具名': 'named',
            '奇特': 'exotic',
            '装备组': 'gearset'
        }
        quality_class = quality_to_class.get(gear['quality'], 'named')

        # ------------------- 6. 准备模板数据 -------------------
        template_data = {
            'gear': {
                'name_zh': gear['name_zh'],
                'name_en': gear['name_en'],
                'part': gear['part'],
                'equipment': gear['equipment'],
                'quality': gear['quality'],
                'quality_class': quality_class,
                'attributes': attributes_list,
                'talent': talent_data is not None,
                'talent_name': talent_data['name_zh'] if talent_data else None,
                'talent_desc': talent_data['description'] if talent_data else None
            },
            'attr_map': attr_map
        }

        # ------------------- 7. 渲染模板 -------------------
        template_path = os.path.join(os.path.dirname(__file__), "templates", "gear_card.html")
        if not os.path.exists(template_path):
            yield event.plain_result("装备卡片模板文件未找到")
            return
        with open(template_path, "r", encoding="utf-8") as f:
            template_str = f.read()
        template = Template(template_str)
        html = template.render(**template_data)

        # ------------------- 8. 生成图片并发送 -------------------
        options = {"type": "png", "full_page": True, "scale": "css", "omit_background": True}
        try:
            img_url = await self.html_render(html, {}, options=options)
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"装备卡片渲染失败: {e}")
            yield event.plain_result("生成图片失败，请稍后重试")

    async def terminate(self):
        pass