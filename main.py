from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig 
from astrbot.api.message_components import Image
from jinja2 import Template
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import re
import time
import json
import aiohttp
import asyncio
import os
import aiosqlite
import difflib

class TheDivision2Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        if config is None:
            base = "http://127.0.0.1:8080"
            self.default_platform = "uplay"
        else:
            base = config.get("api_base_url", "http://127.0.0.1:8080")
            self.default_platform = config.get("default_platform", "uplay")
        self.api_base_url = base.rstrip('/')
        logger.info(f"后端基础地址: {self.api_base_url}, 默认平台: {self.default_platform}")

        # ---------- 性能优化：预加载模板 ----------
        self.templates = {}
        template_files = {
            "player_info": "templates/player_info.html",
            "talent_card": "templates/talent_card.html",
            "weapon_card": "templates/weapon_card.html",
            "equipment_card": "templates/equipment_card.html",
            "gear_card": "templates/gear_card.html",
            "weekly_report": "templates/weekly_report.html",
        }
        for name, path in template_files.items():
            full_path = os.path.join(os.path.dirname(__file__), path)
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    self.templates[name] = Template(f.read())
                logger.debug(f"模板 {name} 加载成功")
            except FileNotFoundError:
                logger.error(f"模板文件未找到: {full_path}")
                self.templates[name] = None

        # ---------- 性能优化：异步预加载名称列表 ----------
        self.name_cache = None  # 未加载状态
        asyncio.create_task(self._load_name_cache_async())

        self.session = aiohttp.ClientSession()

        self.weapon_attributes_map = None  # 初始状态
        asyncio.create_task(self._load_weapon_attributes_map_async())

        self.translations = self._load_translations_sync()

    async def _load_weapon_attributes_map_async(self):
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        if not os.path.exists(db_path):
            self.weapon_attributes_map = {}
            return
        try:
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute("SELECT key, type, entry_name_zh, max_value, named FROM weapon_attributes")
                rows = await cursor.fetchall()
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
            self.weapon_attributes_map = attr_map
            logger.info("武器属性映射加载成功")
        except Exception as e:
            logger.error(f"武器属性映射加载失败: {e}")
            self.weapon_attributes_map = {}

    async def _load_name_cache_async(self):
        """异步加载所有名称到内存缓存"""
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        if not os.path.exists(db_path):
            self.name_cache = {"talent": [], "weapon": [], "gear": [], "equipment_group": []}
            return

        tables = {
            "talent": ["talent", "talents"],
            "weapon": ["weapon"],
            "gear": ["gear"],
            "equipment_group": ["equipment_group"]
        }
        cache = {}
        try:
            async with aiosqlite.connect(db_path) as conn:
                for key, table_list in tables.items():
                    names = []
                    for table in table_list:
                        # 检查表是否存在
                        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
                        if await cursor.fetchone():
                            cursor = await conn.execute(f"SELECT name_zh FROM {table}")
                            rows = await cursor.fetchall()
                            names.extend([row[0] for row in rows if row[0]])
                    cache[key] = names
            self.name_cache = cache
            logger.info("名称缓存加载成功")
        except Exception as e:
            logger.error(f"名称缓存加载失败: {e}")
            self.name_cache = {"talent": [], "weapon": [], "gear": [], "equipment_group": []}

    #天赋查询方法
    async def get_talent_data(self, talent_name: str):
        """根据天赋名称（中文或英文）从 data/data.db 中查询完整信息（异步）"""
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        if not os.path.exists(db_path):
            logger.error(f"数据库文件不存在: {db_path}")
            return None

        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            # 检查表名
            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='talent'")
            row = await cursor.fetchone()
            if row:
                table_name = "talent"
            else:
                cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='talents'")
                row = await cursor.fetchone()
                if row:
                    table_name = "talents"
                else:
                    logger.error("数据库中没有 talent 或 talents 表")
                    return None

            query = f"""
                SELECT name_zh, name_en, `icon path`, type, description
                FROM {table_name}
                WHERE name_zh = ? OR name_en = ?
            """
            cursor = await conn.execute(query, (talent_name, talent_name))
            row = await cursor.fetchone()

        if not row:
            return None

        return {
            "name": row["name_zh"],
            "eng_name": row["name_en"],
            "icon_url": row["icon path"],
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

    # ==================== 搜索建议辅助方法 ====================
    def _get_suggestions(self, table: str, keyword: str, limit: int = 5) -> list:
        """从缓存中获取建议名称列表（同步）"""
        # 如果缓存尚未加载，返回空列表
        if self.name_cache is None:
            return []
        all_names = self.name_cache.get(table, [])
        if not all_names:
            return []

        suggestions = []
        keyword_lower = keyword.lower()

        # 1. difflib 模糊匹配（cutoff=0.4）
        matches = difflib.get_close_matches(keyword, all_names, n=limit, cutoff=0.4)
        suggestions.extend(matches)

        # 2. 包含关系
        for name in all_names:
            if keyword_lower in name.lower() or name.lower() in keyword_lower:
                if name not in suggestions:
                    suggestions.append(name)

        # 3. 共同字符数匹配
        keyword_chars = set(keyword_lower)
        char_matches = []
        for name in all_names:
            if name in suggestions:
                continue
            common = len(keyword_chars & set(name.lower()))
            if common > 0:
                char_matches.append((common, name))
        # 按共同字符数降序排序
        char_matches.sort(key=lambda x: x[0], reverse=True)
        # 补充到 limit 个
        for _, name in char_matches[:limit - len(suggestions)]:
            if name not in suggestions:
                suggestions.append(name)

        # 去重，保留前 limit 个
        seen = set()
        result = []
        for name in suggestions:
            if name not in seen:
                seen.add(name)
                result.append(name)
                if len(result) >= limit:
                    break
        return result

    def _load_translations_sync(self):
        """同步加载翻译文件到内存"""
        trans_path = os.path.join(os.path.dirname(__file__), "translations.json")
        try:
            with open(trans_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载翻译文件失败: {e}")
            return {}

    #武器查询方法
    async def get_weapon_by_name(self, weapon_name: str):
        weapon_name = weapon_name.strip()
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")

        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            # 精确匹配
            cursor = await conn.execute("""
                SELECT name_zh, name_en, type, quality, harm, rpm, magazine_capacity,
                    reload, range, head_magnification, sight, muzzle, grip, magazine,
                    attributes
                FROM weapon
                WHERE name_zh = ? OR name_en = ?
            """, (weapon_name, weapon_name))
            row = await cursor.fetchone()

            if row:
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
            cursor = await conn.execute("SELECT name_zh, alias FROM weapon")
            rows = await cursor.fetchall()
            for row in rows:
                alias_str = row['alias']
                if alias_str:
                    aliases = [a.strip() for a in alias_str.split('\n') if a.strip()]
                    if weapon_name in aliases:
                        # 递归调用，注意加 await
                        return await self.get_weapon_by_name(row['name_zh'])
        return None

    def get_weapon_attributes_map(self):
        """返回缓存的武器属性映射（同步）"""
        if self.weapon_attributes_map is None:
            return {}
        return self.weapon_attributes_map

    async def get_talent_by_weapon_name(self, weapon_name: str):
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT name_zh, name_en, `icon path`, type, description FROM talent WHERE type LIKE ? LIMIT 1",
                (f'%{weapon_name}%',)
            )
            row = await cursor.fetchone()

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
    async def get_equipment_full_data(self, name: str):
        """根据名称或别名查询装备完整信息（包括天赋描述）"""
        db_path = os.path.join(os.path.dirname(__file__), "data", "data.db")
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            # 1. 精确匹配
            cursor = await conn.execute("""
                SELECT 
                    id, name_zh, name_en, alias, type, effect,
                    set_talent,
                    `enhancetalent _ chestarmor` AS enhancetalent_chestarmor,
                    `enhancetalent _ backpack` AS enhancetalent_backpack
                FROM equipment_group 
                WHERE name_zh = ? OR name_en = ?
            """, (name, name))
            row = await cursor.fetchone()

            # 2. 别名匹配
            if not row:
                cursor = await conn.execute("SELECT id, name_zh, alias FROM equipment_group")
                all_rows = await cursor.fetchall()
                for r in all_rows:
                    alias_str = r['alias']
                    if alias_str:
                        aliases = [a.strip() for a in alias_str.split('\n') if a.strip()]
                        if name in aliases:
                            cursor = await conn.execute("""
                                SELECT 
                                    id, name_zh, name_en, alias, type, effect,
                                    set_talent,
                                    `enhancetalent _ chestarmor` AS enhancetalent_chestarmor,
                                    `enhancetalent _ backpack` AS enhancetalent_backpack
                                FROM equipment_group 
                                WHERE id = ?
                            """, (r['id'],))
                            row = await cursor.fetchone()
                            break

            if not row:
                return None

            equipment = dict(row)

            # 3. 如果是装备组，补充天赋描述
            if equipment.get('type') == '装备组':
                talent_map = {}
                fields = ['set_talent', 'enhancetalent_chestarmor', 'enhancetalent_backpack']
                for field in fields:
                    talent_name = equipment.get(field)
                    if talent_name and talent_name != '无':
                        talent_name_clean = talent_name.strip()
                        cursor = await conn.execute("SELECT description FROM talent WHERE name_zh = ?", (talent_name_clean,))
                        desc_row = await cursor.fetchone()
                        if desc_row:
                            talent_map[f"{field}_desc"] = desc_row['description']
                        else:
                            cursor = await conn.execute("SELECT description FROM talent WHERE name_zh LIKE ?", (f'%{talent_name_clean}%',))
                            desc_row = await cursor.fetchone()
                            talent_map[f"{field}_desc"] = desc_row['description'] if desc_row else '暂无描述'
                    else:
                        talent_map[f"{field}_desc"] = None
                equipment.update(talent_map)

            return equipment

    @filter.command("数据查询")
    async def on_query(self, event: AstrMessageEvent, username: str, platform_arg: str = None):
        # 平台映射
        platform_map = {
            'uplay': 'uplay',
            'ubi': 'uplay',
            'xbl': 'xbl',
            'xbox': 'xbl',
            'psn': 'psn',
            'ps': 'psn'
        }

        platform = None
        if platform_arg:
            p = platform_arg.lower().strip()
            platform = platform_map.get(p)

        if not platform:
            platform = self.default_platform

        username = username.strip()
        logger.info(f"解析后平台: {platform}, 玩家标识: {username}")


        # 平台配置（只需用于获取 game_id）
        platforms = {
            "uplay": "60859c37-949d-49e2-8fc8-6d8dc40f1a9e",
            "xbl":   "902e9524-f2bb-4039-8dc5-a36f7d261987",
            "psn":   "8aecdffb-6372-48b6-a684-8085a288069f"
        }
        # 确保平台合法
        if platform not in platforms:
            platform = "uplay"

        game_id = platforms[platform]

        found = False
        player_name = None
        uid = None                # ProfileId，用于后续所有查询和展示
        user_id_for_avatar = None # UserId，仅用于头像

        # 1. 尝试通过玩家名查询指定平台
        profile_url = f"{self.api_base_url}/profile?username={username}&platform={platform}"
        try:
            async with self.session.get(profile_url, timeout=10) as resp:
                    if resp.status == 200:
                        data = (await resp.json()).get("data", {})
                        uid = data.get("ProfileId") or data.get("IdOnPlatform")
                        if uid:
                            player_name = data.get("NameOnPlatform")
                            user_id_for_avatar = data.get("UserId")
                            found = True
                            logger.info(f"通过玩家名 '{username}' 在平台 {platform} 查询成功，ProfileId: {uid}, 玩家名: {player_name}, UserId(头像用): {user_id_for_avatar}")
        except Exception as e:
            logger.warning(f"玩家名查询异常: {e}")

        # 2. 如果未找到，尝试将输入作为 ProfileId 查询
        if not found:
            profile_url = f"{self.api_base_url}/profile?uid={username}&platform={platform}"
            try:
                async with self.session.get(profile_url, timeout=10) as resp:
                        if resp.status == 200:
                            data = (await resp.json()).get("data", {})
                            uid = data.get("ProfileId") or data.get("IdOnPlatform")
                            if uid:
                                player_name = data.get("NameOnPlatform")
                                user_id_for_avatar = data.get("UserId")
                                found = True
                                logger.info(f"通过 ProfileId '{username}' 在平台 {platform} 查询成功，玩家名: {player_name}, ProfileId: {uid}, UserId(头像用): {user_id_for_avatar}")
            except Exception as e:
                logger.warning(f"ProfileId 查询异常: {e}")

        if not found or not uid or not user_id_for_avatar:
            yield event.plain_result(f"未在 {platform} 平台找到该玩家，请检查输入")
            return

        # 使用 ProfileId (uid) 请求统计数据
        stats_url = f"{self.api_base_url}/stats?gameId={game_id}&platform={platform}&uids={uid}"
        try:
            async with self.session.get(stats_url, timeout=10) as resp:
                    if resp.status != 200:
                        yield event.plain_result(f"获取统计数据失败，状态码：{resp.status}")
                        return
                    stats_data = await resp.json()
                    if not stats_data.get("success"):
                        yield event.plain_result(f"统计数据接口返回失败: {stats_data.get('message', '未知错误')}")
                        return
                    data_list = stats_data.get("data", [])
                    if not data_list:
                        yield event.plain_result("未找到该玩家的统计数据")
                        return
        except Exception as e:
            logger.error(f"请求统计异常: {e}")
            yield event.plain_result("网络错误，请稍后重试")
            return

        # 直接取第一个元素（因为只查询了一个 UID）
        player_data = data_list[0].get("stats")
        if not player_data:
            yield event.plain_result("玩家统计数据为空")
            return

        # ========== 数据解析 ==========
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

        #=================== 提取各项数据 ===================
        #头像
        avatar_url = f"https://ubisoft-avatars.akamaized.net/{user_id_for_avatar}/default_146_146.png"
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
        #头部命中率（防除零）
        HeadHitRate = f"{Headshots/hit*100:.1f}%" if hit > 0 else "0.0%"
        #爆头和身体命中比（防除零）
        if Headshots > 0:
            ratio = bodyHit / Headshots
            ratio_str = f"{int(ratio)}" if ratio.is_integer() else f"{ratio:.1f}"
            HeadshotToBodyshotRatio = f"{ratio_str}次身体:1次头部"
        else:
            HeadshotToBodyshotRatio = "0次身体:1次头部"
        #每小时击杀数（防除零）
        KillRatePerHour = int(NpcKills / gametime) if gametime > 0 else 0
        #每小时爆头命中数（防除零）
        HourlyHeadcountHits = int(Headshots / gametime) if gametime > 0 else 0
        #每小时身体命中数（防除零）
        HourlyBodyHits = int(bodyHit / gametime) if gametime > 0 else 0
        #当日击杀数
        DailyKills = player_data.get("npckillsperiodic", {}).get("value", "0")
        #当日爆头数
        DailyHeadcount = player_data.get("DailySumHeadShots", {}).get("value", "0")
        logger.info(f"数据字典已生成，数据来源：UBI-GO URL:{self.api_base_url}")

        #整理字典
        player_info_data = {
            "playername": player_name,
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
        template = self.templates.get("player_info")
        if not template:
            yield event.plain_result("玩家信息模板未加载")
            return
        html = template.render(player_info_data) 
        
        options = {
            "type": "png", 
            "full_page":True,
            "scale":"css",
            "omit_background": True
        }
        imgurl = await self.html_render(html, {}, options=options)
        yield event.plain_result("UID:\n" + uid)
        yield event.image_result(imgurl)  

    @filter.command("周商")
    async def weekly_vendor(self, event: AstrMessageEvent):
        # 缓存文件路径
        cache_dir = Path(get_astrbot_data_path()) / "plugin_data" / self.name / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "weekly_vendor.jpg"
        cache_ttl = 3600  # 1小时

        # 检查缓存是否有效
        if cache_file.exists():
            mtime = cache_file.stat().st_mtime
            if time.time() - mtime < cache_ttl:
                logger.info("使用缓存的周商图片")
                yield event.image_result(str(cache_file))
                return
            else:
                logger.info("缓存已过期，重新生成")
        # 1. 获取原始 JSON 数据
        url = "https://raw.githubusercontent.com/MuFen-mker/astrbot_plugin_TheDivision2_DataAPI/refs/heads/main/all_vendors.json"
        try:
            async with self.session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        yield event.plain_result(f"获取数据失败，状态码：{resp.status}")
                        return
                    data = await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"请求异常：{e}")
            yield event.plain_result("网络错误，请稍后重试")
            return

        # 2. 加载翻译文件
        groups = self.translations
        if not groups:
            yield event.plain_result("翻译文件加载失败")
            return

        # 分类映射表
        armor_map = {}
        weapon_map = {}
        brand_map = {}
        part_map = {}
        attributes_map = {}
        talent_map = {}
        mods_map = {}
        skill_map = {}
        merchant_map = {}
        predefined_id = {}
        attr_max_map = {}

        for group_name, items in groups.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if "en" not in item or "zh" not in item:
                    continue
                if group_name == "armor":
                    armor_map[item["en"]] = item["zh"]
                elif group_name == "weapon":
                    weapon_map[item["en"]] = item["zh"]
                elif group_name == "brand":
                    brand_map[item["en"]] = item["zh"]
                elif group_name == "part":
                    part_map[item["en"]] = item["zh"]
                elif group_name == "attributes":
                    attributes_map[item["en"]] = item["zh"]
                    if "max" in item:
                        key_clean = re.sub(r'\s+', '', item["zh"])
                        attr_max_map[key_clean] = item["max"]
                elif group_name == "talent":
                    talent_map[item["en"]] = item["zh"]
                    if "id" in item:
                        predefined_id[item["en"]] = item["id"]
                elif group_name == "mods":
                    mods_map[item["en"]] = item["zh"]
                elif group_name == "skill":
                    skill_map[item["en"]] = item["zh"]
                elif group_name == "merchant":
                    merchant_map[item["en"]] = item["zh"]

        USE_PREDEFINED_MAX_FOR = ["暴击机率","暴击伤害","爆头伤害","武器控制力","危害防护","爆炸抗性","装甲回复","技能加速","技能伤害","状态效果","修复技能","武器伤害","技能分阶","装甲"]
        USE_PREDEFINED_MAX_FOR_CLEAN = [re.sub(r'\s+', '', name) for name in USE_PREDEFINED_MAX_FOR]

        SPECIAL_ATTRIBUTE2_MAX = {
            "生命值伤害": 21,
            "暴击机率": 21,
            "对装甲的伤害": 12,
            "暴击伤害": 17,
            "爆头伤害": 111,
            "对离开掩体目标的伤害": 12
        }

        SPECIAL_ATTRIBUTE3_MAX = {
            "生命值伤害": 10,
            "暴击机率": 9.5,
            "对装甲的伤害": 6,
            "暴击伤害": 10,
            "爆头伤害": 10,
            "对离开掩体目标的伤害": 10
        }

        # 递归翻译函数（带分类映射）
        def translate_value(obj, context=None, key=None):
            if isinstance(obj, str):
                # 根据字段名和上下文选择映射表
                target_map = None
                if key == 'name':
                    if context == 'gears':
                        target_map = armor_map
                    elif context == 'weapons':
                        target_map = weapon_map
                    elif context == 'mods':
                        target_map = mods_map
                elif key == 'brand':
                    target_map = brand_map
                elif key == 'type':
                    if context == 'mods':
                        target_map = skill_map
                    else:
                        target_map = part_map
                elif key in ('Core', 'attribute1', 'attribute2', 'attribute3', 'attributes'):
                    target_map = attributes_map
                elif key == 'talent':
                    target_map = talent_map

                if target_map:
                    result = obj
                    for en_word in sorted(target_map.keys(), key=len, reverse=True):
                        if en_word in result:
                            result = result.replace(en_word, target_map[en_word])
                    return result
                else:
                    return obj
            elif isinstance(obj, list):
                return [translate_value(item, context, key) for item in obj]
            elif isinstance(obj, dict):
                new_dict = {}
                current_context = context
                for k, v in obj.items():
                    # 更新上下文
                    if k == 'gears':
                        sub_context = 'gears'
                    elif k == 'weapons':
                        sub_context = 'weapons'
                    elif k == 'mods':
                        sub_context = 'mods'
                    else:
                        sub_context = current_context
                    # 递归，并传递字段名 k
                    new_dict[k] = translate_value(v, sub_context, k)

                    # 处理 talent 添加 id
                    if k == "talent":
                        original = v
                        translated = new_dict[k]
                        if original in predefined_id:
                            raw_id = predefined_id[original]
                        elif "Perfect" in original or "Perfectly" in original:
                            id_candidate = re.sub(r'^完美\s*', '', translated)
                            raw_id = id_candidate if id_candidate else translated
                        else:
                            raw_id = translated
                        new_dict["id"] = raw_id.replace(' ', '_')

                    # 处理属性字段添加 max
                    if k in ['Core', 'attribute1', 'attribute2', 'attribute3', 'attributes']:
                        translated_val = new_dict[k]
                        num_match = re.search(r'([\d,]+(?:\.\d+)?)', translated_val)
                        if num_match:
                            raw_num = num_match.group(1).replace(',', '')
                            try:
                                extracted_num = float(raw_num)
                            except ValueError:
                                extracted_num = 0
                        else:
                            extracted_num = 0

                        # 提取属性名称
                        attr_name_clean = None
                        attr_match = re.search(r'\d+(?:\.\d+)?%\s*(.+)', translated_val)
                        if attr_match:
                            attr_name = attr_match.group(1).strip()
                            attr_name_clean = re.sub(r'\s+', '', attr_name)
                        else:
                            name_match = re.search(r'[^\d,\s%]+(?:\s+[^\d,\s%]+)*', translated_val)
                            if name_match:
                                attr_name = name_match.group(0).strip()
                                attr_name_clean = re.sub(r'\s+', '', attr_name)

                        if attr_name_clean:
                            use_predefined = False
                            if current_context == 'gears':
                                if attr_name_clean in USE_PREDEFINED_MAX_FOR_CLEAN:
                                    use_predefined = True
                            else:
                                use_predefined = True

                            if current_context == 'weapons' and k == 'attribute2':
                                is_pistol = False
                                if 'attribute1' in new_dict and "手枪伤害" in new_dict['attribute1']:
                                    is_pistol = True
                                if is_pistol:
                                    rule_map = SPECIAL_ATTRIBUTE3_MAX
                                else:
                                    rule_map = SPECIAL_ATTRIBUTE2_MAX
                                if attr_name_clean in rule_map:
                                    value_num = max(extracted_num, rule_map[attr_name_clean])
                                elif use_predefined and attr_name_clean in attr_max_map:
                                    value_num = max(extracted_num, attr_max_map[attr_name_clean])
                                else:
                                    value_num = extracted_num
                            elif current_context == 'weapons' and k == 'attribute3':
                                if attr_name_clean in SPECIAL_ATTRIBUTE3_MAX:
                                    value_num = max(extracted_num, SPECIAL_ATTRIBUTE3_MAX[attr_name_clean])
                                elif use_predefined and attr_name_clean in attr_max_map:
                                    value_num = max(extracted_num, attr_max_map[attr_name_clean])
                                else:
                                    value_num = extracted_num
                            else:
                                if use_predefined and attr_name_clean in attr_max_map:
                                    value_num = max(extracted_num, attr_max_map[attr_name_clean])
                                else:
                                    value_num = extracted_num
                        else:
                            value_num = extracted_num
                        new_dict[f"{k}_max"] = value_num
                return new_dict
            else:
                return obj

        # 翻译整个数据
        translated_data = translate_value(data)

        # 提取数字和颜色的辅助函数
        def extract_number(s):
            match = re.search(r'([\d,]+(?:\.\d+)?)', s)
            if match:
                return float(match.group(1).replace(',', ''))
            return 0

        def get_attribute_color(attr_str):
            if any(kw in attr_str for kw in ['装甲回复', '危害防护', '爆炸抗性', '生命值']):
                return '#289eff'
            elif any(kw in attr_str for kw in ['爆头伤害', '暴击机率', '暴击伤害', '生命值伤害', '对装甲的伤害', '对离开掩体目标的伤害', '武器伤害', '手枪伤害', '武器控制力', '准确度', '稳定度', '弹药容量']):
                return '#ff4242'
            elif any(kw in attr_str for kw in ['技能加速', '技能伤害', '修复技能', '技能持续时间']):
                return '#f6ff42'
            else:
                return '#fba000'

        # 预处理数据（添加数值、颜色等）
        for vendor_data in translated_data.values():
            # 护甲
            for gear in vendor_data.get('gears', []):
                gear['Core_value'] = extract_number(gear['Core'])
                if '装甲' in gear['Core']:
                    gear['gradient_color'] = '#289eff'
                    gear['Core_color'] = '#289eff'
                elif '武器伤害' in gear['Core']:
                    gear['gradient_color'] = '#ff2e2e'
                    gear['Core_color'] = '#ff2e2e'
                elif '技能分阶' in gear['Core']:
                    gear['gradient_color'] = '#f18600'
                    gear['Core_color'] = '#f18600'
                else:
                    gear['gradient_color'] = '#289eff'
                    gear['Core_color'] = '#289eff'

                if gear.get('attribute1') and gear['attribute1'] != '-':
                    gear['attribute1_value'] = extract_number(gear['attribute1'])
                    gear['attribute1_color'] = get_attribute_color(gear['attribute1'])
                if gear.get('attribute2') and gear['attribute2'] != '-':
                    gear['attribute2_value'] = extract_number(gear['attribute2'])
                    gear['attribute2_color'] = get_attribute_color(gear['attribute2'])

            # 武器
            for weapon in vendor_data.get('weapons', []):
                for attr in ['attribute1', 'attribute2', 'attribute3']:
                    if weapon.get(attr) and weapon[attr] != '-':
                        weapon[f'{attr}_value'] = extract_number(weapon[attr])

            # 模组
            for mod in vendor_data.get('mods', []):
                if mod.get('type') == '护甲模组':
                    mod['attributes_value'] = extract_number(mod['attributes'])
                    mod['attributes_color'] = get_attribute_color(mod['attributes'])
                    if '攻击协定' in mod['name']:
                        mod['gradient_color'] = '#770000'
                        mod['icon_prefix'] = '攻击协定'
                    elif '防御协定' in mod['name']:
                        mod['gradient_color'] = '#003f73'
                        mod['icon_prefix'] = '防御协定'
                    elif '性能协定' in mod['name']:
                        mod['gradient_color'] = '#ab5f00'
                        mod['icon_prefix'] = '性能协定'
                    else:
                        mod['gradient_color'] = '#fba000'
                        mod['icon_prefix'] = '护甲模组'

        # 加载模板并渲染
        template = self.templates.get("weekly_report")
        if not template:
            yield event.plain_result("周商模板未加载")
            return
        html = template.render(data=translated_data, vendor_name_map=merchant_map)

        options = {
            "type": "jpeg",
            "quality": 75,
            "device_scale_factor": 1,
        }
        try:
            img_url = await self.html_render(html, {},options=options)  # 可加 options
        except Exception as e:
            logger.error(f"图片渲染失败: {e}")
            yield event.plain_result("生成图片失败，请稍后重试")
            return

        # 下载图片到缓存文件
        try:
            async with self.session.get(img_url) as resp:
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
        talent = await self.get_talent_data(talent_name.strip())
        if not talent:
            suggestions = self._get_suggestions("talent", talent_name.strip())
            if suggestions:
                msg = f"🤔未找到名为「{talent_name}」的天赋。\n你可能想找\n" + "\n".join([f"• {s}" for s in suggestions])
                yield event.plain_result(msg)
                return
            yield event.plain_result(f"🤔未找到名为「{talent_name}」的天赋")
            return

        template = self.templates.get("talent_card")
        if not template:
            yield event.plain_result("天赋卡片模板未加载")
            return
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

        # 查询武器
        weapon = await self.get_weapon_by_name(weapon_name)
        if not weapon:
            suggestions = self._get_suggestions("weapon", weapon_name)
            if suggestions:
                msg = f"🤔未找到名为「{weapon_name}」的武器。\n你可能想找\n" + "\n".join([f"• {s}" for s in suggestions])
                yield event.plain_result(msg)
                return
            yield event.plain_result(f"🤔未找到名为「{weapon_name}」的武器")
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

        talent = await self.get_talent_by_weapon_name(weapon['name_zh'])

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
        template = self.templates.get("weapon_card")
        if not template:
            yield event.plain_result("武器卡片模板未加载")
            return
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

        equipment = await self.get_equipment_full_data(name.strip())
        if not equipment:
            suggestions = self._get_suggestions("equipment_group", name.strip())
            if suggestions:
                msg = f"🤔未找到名为「{name}」的装备组/品牌。\n你可能想找\n" + "\n".join([f"• {s}" for s in suggestions])
                yield event.plain_result(msg)
                return
            yield event.plain_result(f"🤔未找到名为「{name}」的装备组/品牌")
            return

        # 渲染模板
        template = self.templates.get("equipment_card")
        if not template:
            yield event.plain_result("装备卡片模板未加载")
            return
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
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row

            # 1. 查询装备（支持别名）
            cursor = await conn.execute("SELECT * FROM gear WHERE name_zh = ? OR name_en = ?", (gear_name, gear_name))
            row = await cursor.fetchone()
            if not row:
                # 别名匹配
                cursor = await conn.execute("SELECT name_zh, alias FROM gear")
                rows = await cursor.fetchall()
                for r in rows:
                    alias_str = r['alias']
                    if alias_str:
                        aliases = [a.strip() for a in alias_str.split('\n') if a.strip()]
                        if gear_name in aliases:
                            cursor = await conn.execute("SELECT * FROM gear WHERE name_zh = ?", (r['name_zh'],))
                            row = await cursor.fetchone()
                            break
            if not row:
                # 关闭连接后查询建议
                suggestions = self._get_suggestions("gear", gear_name)
                if suggestions:
                    msg = f"🤔未找到名为「{gear_name}」的装备。\n你可能想找\n" + "\n".join([f"• {s}" for s in suggestions])
                    yield event.plain_result(msg)
                    return
                yield event.plain_result(f"🤔未找到名为「{gear_name}」的装备")
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

            # 2. 查询 gear_attributes 映射
            cursor = await conn.execute("SELECT key, type, icon, entry_name_zh, max_value, named FROM gear_attributes")
            attr_rows = await cursor.fetchall()
            attr_map = {}
            for ar in attr_rows:
                attr_map[ar['key']] = {
                    'type': ar['type'],
                    'icon': ar['icon'],
                    'entry_name_zh': ar['entry_name_zh'],
                    'max_value': ar['max_value'],
                    'named': ar['named']   # "TRUE"/"FALSE"
                }

            # 3. 查询天赋（如果 gear.talent 为 "TRUE"）
            talent_data = None
            if gear.get('talent') == 'TRUE':
                cursor = await conn.execute("SELECT name_zh, name_en, `icon path`, description FROM talent WHERE type LIKE ? LIMIT 1", (f'%{gear["name_zh"]}%',))
                t_row = await cursor.fetchone()
                if t_row:
                    talent_data = {
                        'name_zh': t_row['name_zh'],
                        'name_en': t_row['name_en'],
                        'icon_path': t_row['icon path'],
                        'description': t_row['description']
                    }

        # 构建属性列表（与原代码相同）
        attributes_list = []
        for attr_key in gear['attributes']:
            info = attr_map.get(attr_key, {})
            max_value_raw = info.get('max_value', '0')
            attributes_list.append({
                'key': attr_key,
                'name': info.get('entry_name_zh', attr_key),
                'icon': info.get('icon', '武器属性.png'),
                'max_value': max_value_raw,
                'named': info.get('named', 'FALSE') == 'TRUE'
            })

        # 品质映射
        quality_to_class = {
            '具名': 'named',
            '奇特': 'exotic',
            '装备组': 'gearset'
        }
        quality_class = quality_to_class.get(gear['quality'], 'named')

        # 准备模板数据
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

        # 渲染模板（与原代码相同）
        template = self.templates.get("gear_card")
        if not template:
            yield event.plain_result("装备卡片模板未加载")
            return
        html = template.render(**template_data)

        options = {"type": "png", "full_page": True, "scale": "css", "omit_background": True}
        try:
            img_url = await self.html_render(html, {}, options=options)
            yield event.image_result(img_url)
        except Exception as e:
            logger.error(f"装备卡片渲染失败: {e}")
            yield event.plain_result("生成图片失败，请稍后重试")

    async def terminate(self):
        if hasattr(self, 'session'):
            await self.session.close()