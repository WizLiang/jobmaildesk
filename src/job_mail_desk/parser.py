from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from email.utils import parseaddr
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .identity_dictionaries import IdentityDictionaries
from .models import MailRecord, ParsedEvent
from .normalization import (
    canonical_company, canonical_role, is_invalid_role,
    strip_one_safe_company_recruitment_suffix,
)
from .privacy import redact_text


SHANGHAI = ZoneInfo("Asia/Shanghai")
# Bump whenever parsing/identity behavior changes. StateStore uses this value
# to replay the bounded mailbox lookback once instead of incorrectly treating
# every message as already handled by an older release.
PARSER_VERSION = "2026.09.20.1"
URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+")
RECRUITING_KEYWORDS = (
    "笔试",
    "考试",
    "测评",
    "AI面试",
    "AI 面试",
    "面试",
    "初面",
    "一面",
    "二面",
    "三面",
    "四面",
    "五面",
    "终面",
    "材料提交",
    "截止",
    "校园招聘",
    "校招",
    "秋招",
    "春招",
    "招聘",
    "应聘",
    "offer",
    "录用",
    "未通过",
    "感谢投递",
    "感谢您投递",
    "感谢你投递",
    "投递成功",
    "申请成功",
    "网申成功",
    "收到你的申请",
    "收到您的申请",
    "完善简历",
    "岗位名称",
    "应聘岗位",
)
NON_RECRUITING_SUBJECT = re.compile(
    r"(?:短信|邮箱|邮件|账户|帐号|登录)?验证码|"
    r"注册邮箱激活码|"
    r"面试(?:满意度|体验)(?:调查|问卷)",
    re.IGNORECASE,
)
TRANSACTIONAL_SUBJECT = re.compile(
    r"在线(?:笔试|考试|测评)|"
    r"(?:笔试|考试|测评|AI面试|AI 面试|面试|初面|一面|二面|三面|四面|五面|终面)"
    r"(?:邀请|邀约|安排|通知|确认)|"
    r"(?:申请|投递|网申)(?:成功|确认)|"
    r"应聘结果|未通过|录用通知|offer",
    re.IGNORECASE,
)
STAGES = (
    ("简历完善", ("完善简历信息", "完善简历", "更新简历信息")),
    ("Offer", ("offer", "录用通知", "录用意向")),
    ("未通过", ("未通过", "遗憾通知", "不匹配")),
    (
        "简历筛选",
        ("简历评估通过", "简历筛选通过", "通过简历评估", "通过简历筛选"),
    ),
    ("在线笔试", ("在线笔试", "在线考试", "岗位的考试", "职位的考试", "笔试")),
    ("人才测评", ("人才测评", "在线测评", "测评")),
    ("AI 面试", ("AI面试", "AI 面试")),
    ("HR 面试", ("HR面", "HR 面", "人力面试")),
    (
        "面试",
        (
            "面试邀请",
            "面试安排",
            "业务面",
            "初面",
            "一面",
            "二面",
            "三面",
            "四面",
            "五面",
            "终面",
            "面试",
        ),
    ),
    ("材料截止", ("材料提交", "材料补充", "提交材料")),
    (
        "网申",
        (
            "网申",
            "申请成功",
            "投递成功",
            "感谢投递",
            "感谢您投递",
            "感谢你投递",
        ),
    ),
)
FULL_RANGE = re.compile(
    r"(?P<sy>20\d{2})[年./-](?P<sm>\d{1,2})[月./-](?P<sd>\d{1,2})[日号]?"
    r"[^\d]{0,12}(?P<sh>[01]?\d|2[0-3]|24)[:：](?P<smin>[0-5]\d)(?::[0-5]\d)?"
    r"\s*(?:至|到|[-–—]{1,2}|[~～])\s*"
    r"(?P<ey>20\d{2})[年./-](?P<em>\d{1,2})[月./-](?P<ed>\d{1,2})[日号]?"
    r"[^\d]{0,12}(?P<eh>[01]?\d|2[0-3]|24)[:：](?P<emin>[0-5]\d)(?::[0-5]\d)?"
)
PARTIAL_CROSS_RANGE = re.compile(
    r"(?P<sy>20\d{2})[年./-](?P<sm>\d{1,2})[月./-](?P<sd>\d{1,2})[日号]?"
    r"[^\d]{0,12}(?P<sh>[01]?\d|2[0-3]|24)[:：](?P<smin>[0-5]\d)"
    r"\s*(?:至|到|[-–—]{1,2}|[~～])\s*"
    r"(?P<em>\d{1,2})[月./-](?P<ed>\d{1,2})[日号]?"
    r"[^\d]{0,12}(?P<eh>[01]?\d|2[0-3]|24)[:：](?P<emin>[0-5]\d)"
)
SAME_DAY_RANGE = re.compile(
    r"(?:(?P<y>20\d{2})[年./-])?(?P<m>\d{1,2})[月./-](?P<d>\d{1,2})[日号]?"
    r"[^\d]{0,12}(?P<sh>[01]?\d|2[0-3]|24)[:：](?P<smin>[0-5]\d)(?::[0-5]\d)?"
    r"\s*(?:至|到|[-–—]{1,2}|[~～])\s*"
    r"(?P<eh>[01]?\d|2[0-3]|24)[:：](?P<emin>[0-5]\d)(?::[0-5]\d)?"
)
DATETIME = re.compile(
    r"(?:(?P<y>20\d{2})[年./-])?(?P<m>\d{1,2})[月./-](?P<d>\d{1,2})[日号]?"
    r"[^\d]{0,12}(?P<h>[01]?\d|2[0-3]|24)[:：](?P<min>[0-5]\d)(?::[0-5]\d)?"
)
CN_DATETIME = re.compile(
    r"(?:(?P<y>20\d{2})[年./-])?(?P<m>\d{1,2})[月./-](?P<d>\d{1,2})[日号]?"
    r"[^\d]{0,12}(?P<h>[01]?\d|2[0-3]|24)\s*点(?:\s*(?P<min>[0-5]?\d)\s*分)?"
)
RELATIVE_DEADLINE = re.compile(
    r"(?:收到(?:本次?)?(?:邮件|通知|链接|短信)(?:作答通知)?(?:后)?(?:的)?|"
    r"请在|请于|请您于|请你于|须在|务必在|务必于|建议(?:您|你)?在)"
    r"[^。；;]{0,20}?"
    r"(?P<amount>\d{1,3}|[一二两三四五六七八九十百]+)\s*"
    r"(?P<unit>个?工作日|小时|天|日)(?:内|之内)"
)
# "链接将于 24 小时后失效" states the same deadline from the other direction.
RELATIVE_EXPIRY = re.compile(
    r"将(?:于|在)\s*"
    r"(?P<amount>\d{1,3}|[一二两三四五六七八九十百]+)\s*"
    r"(?P<unit>个?工作日|小时|天|日)\s*(?:之?后)\s*"
    r"(?:失效|过期|关闭|截止|结束)"
)
DURATION = re.compile(
    r"(?:面试|测评|笔试|预计)?时长\s*[:：]?\s*(?P<minutes>\d{1,3})\s*(?:分钟|min(?:ute)?s?)",
    re.IGNORECASE,
)
DURATION_HOURS = re.compile(
    r"(?:面试|测评|笔试|预计)?时长\s*[:：]?\s*"
    r"(?P<hours>\d{1,2}(?:\.\d+)?)\s*小时",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FieldCandidate:
    value: str
    source: str
    confidence: float
    evidence_tier: int
    raw_value: str | None = None


@dataclass(frozen=True)
class FieldSelection:
    value: str | None
    confidence: float
    source: str | None
    raw_value: str | None = None


# From headers that name a recruiter, an applicant-tracking product, or a job
# board rather than the employer. Treating these as the company attaches the
# application to "温煦" or "boss直聘" instead of the firm actually hiring.
NON_EMPLOYER_SENDER = re.compile(
    r"^(?:"
    r"recruit(?:ment|ing)?|hr|no-?reply|noreply|admin(?:istrator)?|system|"
    r"campus|exam|mailer|postmaster|service|support|notification"
    r")$"
    r"|^(?:系统|管理员|招聘|校园招聘|招聘助手|招聘小秘书|人事|人力资源)$"
    r"|iTalent|北森|boss直聘|BOSS直聘|猎聘|智联|前程无忧|牛客|实习僧|应届生"
    r"|_exam$|^exam_"
    # Latin given names and free-form personal handles such as "Amy" or
    # "andyoffercome zhu"; a real employer survives via subject or body.
    r"|^[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z.'-]+){0,2}$",
    re.IGNORECASE,
)
# Words that mark a name as an organisation. "小鹏汽车" is a firm; "温煦" is the
# recruiter who happens to be sending on its behalf.
COMPANY_ENTITY_TOKEN = re.compile(
    r"科技|汽车|集团|电子|半导体|信息|微电|股份|有限|公司|网络|软件|智能|"
    r"通信|系统|数据|医药|银行|证券|能源|材料|研究院|实验室|工业|制造|"
    r"生物|医疗|传感|存储|技术|互娱|游戏|文化|传媒|地产|物流|保险|资本|"
    r"电器|机械|装备|化学|环境|航天|航空|微|芯"
)
_CHINESE_SURNAMES = (
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖"
    "田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石贾夏韦付方"
    "白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
    "温栾廖章聂晏骆奚"
)
CHINESE_PERSONAL_NAME = re.compile(
    rf"^[{_CHINESE_SURNAMES}][\u4e00-\u9fff]{{1,2}}$"
)


def _is_personal_sender_name(name: str) -> bool:
    """Whether a From display name is a recruiter rather than the employer."""
    stripped = name.strip()
    if not stripped:
        return False
    if NON_EMPLOYER_SENDER.search(stripped):
        return True
    if COMPANY_ENTITY_TOKEN.search(stripped):
        return False
    return bool(
        NON_EMPLOYER_SENDER.search(stripped)
        or CHINESE_PERSONAL_NAME.match(stripped)
    )

# Campus venues, document titles, and the applicant's own name all show up in
# the same brackets and prefixes that usually hold the employer.
NON_EMPLOYER_LABEL = re.compile(
    r"大学|学院|校区|学校|中学|附中|职院"
    r"|邀请函|通知书|温馨提示|致同学|同学"
    r"|^(?:邀请|通知|提醒|公告|报名|面试|笔试|测评)$",
)
COMPANY_NOISE = re.compile(
    r"快来|Get最新|招聘信息|查询方式|管理系统|请尽快|点击|通知|"
    r"招聘系统|您已应聘|你已应聘|已应聘|应聘记录|个人中心",
    re.IGNORECASE,
)
ROLE_NOISE = re.compile(
    r"招聘信息|查询方式|管理系统|个人中心|应聘记录|点击|快来|Get最新",
    re.IGNORECASE,
)
ROLE_COHORT_SUFFIX = re.compile(
    r"\s*[【\[(（]?\s*(?:20)?\d{2}届?\s*"
    r"(?:校园招聘|校招|秋招|春招|提前批|正式批)\s*[】\])）]?\s*$",
    re.IGNORECASE,
)
ROLE_COHORT_PREFIX = re.compile(
    r"^\s*(?:20)?\d{2}届?(?:校园招聘|校招|秋招|春招)"
    r"\s*[-—·_/:：]\s*",
    re.IGNORECASE,
)
ROLE_BEFORE_COHORT_STAGE = re.compile(
    r"(?:邀请|诚邀)(?:您|你)?参加\s*"
    r"(?P<role>[^，。\n]{2,80}?)\s*"
    r"[【\[(（]\s*(?:20)?\d{2}届?\s*"
    r"(?:校园招聘|校招|秋招|春招|提前批|正式批)\s*[】\])）]\s*"
    r"(?:在线)?(?:笔试|测评|面试)",
    re.IGNORECASE,
)
ROLE_AFTER_COHORT_STAGE = re.compile(
    r"[【\[(（]\s*(?:20)?\d{2}届?\s*"
    r"(?:校园招聘|校招|秋招|春招|提前批|正式批)\s*[】\])）]\s*"
    r"(?P<role>[^【\[\]】，。\n]{2,80}?)\s*"
    r"(?:[-—]\s*)?(?:的\s*)?(?:AI\s*)?(?:笔试|测评|面试)",
    re.IGNORECASE,
)
ROLE_COHORT_TO_STAGE = re.compile(
    r"(?:20)?\d{2}届?(?:校园招聘|校招)"
    r"(?!\s*[】\])）])\s*"
    r"(?P<role>[^，。\n]{2,80}?)"
    r"(?:简历评估|简历筛选|在线笔试|笔试|测评|面试)",
    re.IGNORECASE,
)
SUBJECT_COHORT_ROLE_STAGE = re.compile(
    r"[【\[(（]\s*(?:20)?\d{2}届?\s*"
    r"(?:校园招聘|校招|秋招|春招|提前批|正式批)\s*[】\])）]\s*"
    r"(?P<role>[^，。\n]{2,80}?)\s*"
    r"(?:[-—]\s*)?(?:AI\s*)?(?:笔试|测评|面试)",
    re.IGNORECASE,
)
ROLE_ASSESSMENT_APPLICATION = re.compile(
    r"感谢(?:您|你)?投递\s*[^，。\n]{2,60}?\s*的\s*"
    r"(?P<role>[^，。\n]{2,100}?)(?:[，,。])"
    r".{0,100}?(?:通知|邀请).{0,40}?(?:考试|笔试|测评|面试)",
    re.IGNORECASE,
)
# Legal names can include locations in parentheses and need not be in the
# reviewed dictionary. Match an explicit invitation, never a random prose span.
LEGAL_COMPANY_NAME = (
    r"[\u4e00-\u9fffA-Za-z0-9·.&()（）\- ]{2,60}?"
    r"(?:股份有限公司|有限责任公司|有限公司)"
)
SUBJECT_EXAM_COMPANY_ROLE = re.compile(
    rf"^(?:请(?:您|你)?参加|邀请(?:您|你)?参加)\s*"
    rf"(?P<company>{LEGAL_COMPANY_NAME})\s*的\s*"
    r"(?P<role>[^，。；;！!\n]{2,100}?)\s*"
    r"(?:线上|在线|视频)?(?:考试|笔试|测评|(?:AI\s*)?面试)"
    r"(?:邀请|通知)?[！!。]?$", re.IGNORECASE,
)
SUBJECT_LEGAL_COMPANY_STAGE = re.compile(
    rf"^(?!来自|请|邀请|感谢|欢迎|尊敬)(?P<company>{LEGAL_COMPANY_NAME})\s*[-—–_｜|:：]?\s*"
    r"(?:(?:20)?\d{2}届?(?:校园招聘|校招|秋招|春招))?\s*的?\s*"
    r"(?:线上|在线|视频|AI\s*)?(?:笔试|面试|测评|考试)"
    r"(?:邀请|邀约|通知|安排)(?:[！!。]|$)", re.IGNORECASE,
)
ROLE_QUOTED_SCREENING = re.compile(
    r"(?:您|你)?的?简历(?:已)?通过(?:了)?(?:我司|我公司|本公司|本司)\s*"
    r"[\"“「『](?P<role>[^\"“”「」『』\n，。]{2,100})[\"”」』]\s*"
    r"(?:岗位|职位)?的?(?:简历)?(?:筛选|初筛|评估)"
)
ROLE_TITLE_HINT = re.compile(
    r"工程师|设计师|架构师|分析师|研究员|经理|专员|助理|管培|"
    r"开发|算法|运营|销售|产品|财务|会计|法务|顾问|岗(?:位)?$|HRBP", re.IGNORECASE,
)
ROLE_PATTERNS = (
    re.compile(
        r"(?:面试职位|应聘职位|职位名称|应聘岗位|岗位名称|意向岗位|岗位|职位)"
        r"\s*[:：]\s*([^\n，,。；;]{2,100})"
    ),
    re.compile(
        r"感谢(?:您|你)?投递(?:我|本)公司的\s*"
        r"(?:【[^】]{2,30}】\s*)?[「]?([^，。\n」]{2,100}?)[」]?\s*职位"
    ),
    re.compile(
        r"(?:您|你)?已成功投递\s*"
        r"[【「]?([^，。\n】」]{2,100}?)[】」]?\s*职位"
    ),
    re.compile(
        r"(?:现)?(?:诚邀|邀请)(?:您|你)?参加\s*"
        r"[【「]?([^，。\n】」]{2,100}?)[】」]?\s*(?:岗位|职位)"
    ),
    re.compile(
        r"(?:现)?邀(?:请)?(?:您|你)参加\s*"
        r"[【「]?([^，。\n】」]{2,100}?)[】」]?\s*(?:岗位|职位)"
    ),
    re.compile(
        r"^【(?:NIO蔚来|蔚来NIO)】感谢(?:您|你)?的?投递[！!：:\s—-]*"
        r"((?:提前批|正式批)?[-—]?[^\n，。！!]{2,100})$",
        re.MULTILINE,
    ),
    re.compile(r"(?:诚邀|邀请)(?:您|你)?参加\s*[「【]([^」】]{2,100})[」】]"),
    re.compile(
        r"已收到(?:您|你)?对\s*[「【]?([^，。\n」】]{2,100}?)[」】]?\s*的申请"
    ),
    re.compile(
        r"感谢(?:您|你)?投递\s*[^，。\n]{2,60}?\s*的\s*"
        r"([^，。\n]{2,100}?)(?:[，,。]|$)"
    ),
    re.compile(
        r"(?:20)?\d{2}届?(?:校园招聘|校招)"
        r"(?!\s*[】\])）])\s*"
        r"([^【\[\]】，。\n]{2,80}?)"
        r"(?:简历评估|简历筛选|在线笔试|笔试|测评|面试)"
    ),
    re.compile(r"(?:邀请您参加|邀请你参加)\s*([^，。\n]{2,60}?)\s*岗位"),
    re.compile(r"(?:您|你)投递的\s*([^，。\n]{2,60}?)\s*职位"),
    re.compile(r"感谢您投递[^\n，。]{2,120}[）)]([^，。]{2,40})，现邀请"),
    re.compile(
        r"(?:您|你)?已成功申请\s*[【「]?\s*"
        r"([^，。\n】」]{2,100}?)\s*[】」]?\s*职位"
    ),
    re.compile(
        r"感谢(?:您|你)?投递[^，。\n]{2,80}?有限公司(?:公司)?的\s*"
        r"([^，。\n]{2,100}?)(?:职位|岗位)"
    ),
    re.compile(
        r"(?:您|你)申请的\s*[【「]([^】」\n]{2,100})[】」]\s*岗位"
    ),
    re.compile(
        r"(?:您|你)在\s*([^，。\n]{2,100}?)\s*"
        r"(?:-\s*[^，。\n]{2,40})?上的投递"
    ),
    re.compile(
        r"感谢(?:您|你)?投递[^，。\n]{2,50}?"
        r"(?:校园招聘|校招)\s*([^，。\n]{2,100}?)(?:岗位|职位)"
    ),
    re.compile(
        r"(?:并|已)投递\s*([^，。\n]{2,100}?)\s*[，,]\s*"
        r"(?:现|并)?(?:诚邀|邀请)"
    ),
)

LOCATION_NAMES = (
    "北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都", "武汉",
    "西安", "重庆", "天津", "长沙", "合肥", "厦门", "福州", "宁波", "无锡",
    "青岛", "济南", "郑州", "东莞", "佛山", "珠海", "大连", "沈阳", "昆明",
    "南昌", "南宁", "贵阳", "海口", "石家庄", "太原", "哈尔滨", "长春",
    "乌鲁木齐", "呼和浩特", "兰州", "银川", "西宁", "香港", "澳门", "台北",
    "常州", "惠州", "昆山", "嘉兴", "绍兴", "烟台", "南通", "徐州", "温州",
)
LOCATION_TOKEN = "|".join(sorted(LOCATION_NAMES, key=len, reverse=True))
LOCATION_LABEL = re.compile(
    rf"(?<!公司)(?<!面试)"
    rf"(?:工作地点|岗位地点|办公地点|工作城市|办公城市|职位地点|工作地)"
    rf"\s*[:：]\s*(?P<value>(?:(?:{LOCATION_TOKEN})(?:市)?"
    rf"(?:\s*[/、,，及和或]\s*)?)+)"
)
ROLE_LOCATION_SUFFIX = re.compile(
    rf"(?:[-—–｜|/（(]\s*)"
    rf"(?P<value>(?:(?:{LOCATION_TOKEN})(?:市)?"
    rf"(?:\s*[/、,，及和或]\s*)?)+)\s*(?:岗(?:位)?)?\s*[）)]?"
rf"(?=\s*(?:[（(【\[]?\s*[A-Za-z]{{1,4}}\d{{4,}}\s*[）)】\]]?)?\s*$)"
)
ROLE_JOB_CODE = re.compile(
    r"(?<![A-Za-z0-9])(?P<code>[A-Za-z]{1,4}\d{4,})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
ROLE_JOB_CODE_SUFFIX = re.compile(
    r"\s*(?:[-—·_/]\s*)?[（(【\[]?\s*[A-Za-z]{1,4}\d{4,}\s*[）)】\]]?\s*$",
    re.IGNORECASE,
)
ROLE_JOB_CODE_PREFIX = re.compile(
    r"^\s*[（(【\[]?\s*[A-Za-z]{1,4}\d{4,}\s*[）)】\]]?"
    r"\s*[-—·_/:：]\s*",
    re.IGNORECASE,
)
ROLE_BASE_LOCATION_COHORT = re.compile(
    rf"[（(]\s*base\s*(?P<value>{LOCATION_TOKEN})(?:市)?"
    rf"(?:\s*[-—]\s*(?:20)?\d{{2}}届?(?:校园招聘|校招|秋招|春招))?"
    rf"\s*[）)]",
    re.IGNORECASE,
)


def normalize(value: str) -> str:
    normalized = value.replace("\u3000", " ")
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_multiline(value: str) -> str:
    normalized = value.replace("\u3000", " ")
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )
    return "\n".join(
        re.sub(r"[^\S\n]+", " ", line).strip()
        for line in normalized.splitlines()
    ).strip()


EXPLICIT_APPLICATION_SUBJECT = re.compile(
    r"(?:感谢(?:您|你)?\s*投递.{2,160}?(?:职位|岗位)|"
    r"(?:已成功申请|申请成功).{0,120}?(?:职位|岗位))",
    re.IGNORECASE,
)
SUBJECT_APPLICATION_COMPANY_ROLE = re.compile(
    r"感谢(?:您|你)?\s*投递\s*"
    r"(?P<company>[^，。\n]{2,60}?)(?:公司)?的\s*"
    r"(?P<role>[^，。\n]{2,100}?)(?:职位|岗位)",
    re.IGNORECASE,
)


RECRUITING_ANNOUNCEMENT = re.compile(
    r"宣讲|空宣|双选会|招聘会|智联推荐|(?:岗位|职位|工作)推荐|"
    r"(?:诚邀|邀请|邀)[您你]?\s*(?:来|踊跃)?\s*投递|欢迎投递|"
    r"(?:火热|踊跃|立即)报名|"
    r"(?:校招|校园招聘|秋招|春招)[^。！？!?]{0,24}(?:启动|开启|来袭)|"
    r"(?:招聘|校招)[^。！？!?]{0,24}(?:简章|公告|推荐)",
    re.IGNORECASE,
)
# A venue, a date, a salutation or the word 'offer' is not evidence that the
# recipient has applied. Keep actual receipts and personal next-step notices.
PERSONAL_NOTICE_SUBJECT = re.compile(
    r"(?:笔试|考试|测评|面试|初面|终面)(?:邀请|邀约|安排|通知|确认|结果)|"
    r"(?:申请|投递|网申)(?:成功|确认)|应聘(?:结果|进度)|"
    r"(?:offer|录用)(?:通知|意向|确认|函)|未通过|"
    r"(?:感谢(?:您|你)?\s*投递|(?:您|你)已(?:成功)?(?:申请|应聘|投递))",
    re.IGNORECASE,
)
PERSONAL_PROGRESS = re.compile(
    r"(?:已收到|收到|已接收)(?:您|你)的.{0,60}?(?:申请|简历|投递)|"
    r"感谢(?:您|你)?\s*(?:投递|申请).{0,80}?(?:职位|岗位)|"
    r"(?:您|你)(?:的.{0,40}?)?(?:已|已经)(?:成功)?(?:申请|投递|通过|进入)|"
    r"(?:您|你)的.{0,35}?(?:面试|笔试|测评|考试)(?:开始)?时间|"
    r"(?:您|你)的.{0,35}?(?:简历|申请).{0,20}?(?:通过|未通过|收到|审核|评估)",
    re.IGNORECASE,
)


ONSITE_SESSION_VENUE = re.compile(
    r"(?:宣讲(?:会)?)?(?:地\s*[点址]|场地)[】\]]?\s*[:：]?\s*"
    r"(?![^\n。；]{0,20}(?:线上|直播|视频|腾讯会议|飞书|钉钉|zoom))"
    r"[^\n。；]{0,60}?(?:校区|学院|教学楼|报告厅|礼堂|会议室|活动中心|"
    r"教\s*[一二三四五六七八九十\d]|楼\s*[A-Za-z\d]|馆)",
    re.IGNORECASE,
)


def is_recruiting_marketing(subject: str, body: str) -> bool:
    """Suppress broadcasts without suppressing a recipient's hiring steps."""
    subject, body = normalize(subject), normalize(body)
    if not RECRUITING_ANNOUNCEMENT.search(f"{subject} {body}"):
        return False
    if EXPLICIT_APPLICATION_SUBJECT.search(subject):
        return False
    # A real notice can carry a marketing footer. If the title itself is a
    # broadcast, require stronger personal evidence from its body instead.
    if (
        not RECRUITING_ANNOUNCEMENT.search(subject)
        and PERSONAL_NOTICE_SUBJECT.search(subject)
    ):
        return False
    for sentence in re.split(r"[。！？!?；;]", body):
        # A broadcast's conditional instructions ("若您已投递，请忽略")
        # do not establish that this recipient has actually applied.
        if PERSONAL_PROGRESS.search(sentence) and not re.search(r"如果|假如|若|如您|如你", sentence):
            return False
    if (
        re.search(r"(?:欢迎|感谢)(?:您|你)应聘.{0,60}?(?:岗位|职位)", body)
        and re.search(
            r"(?:邀请|诚邀)(?:您|你).{0,35}?参加[^。！？!?]{0,35}(?:笔试|面试|测评|考试)",
            body,
        )
    ):
        return False
    return True


def parser_diagnostics(record: MailRecord) -> dict[str, int | bool]:
    """Return privacy-safe reasons for a noncandidate parser decision."""
    subject = normalize(record.subject)
    combined = normalize(f"{subject} {record.body}")
    return {
        "subject_length": len(subject),
        "body_length": len(record.body),
        "content_truncated": record.content_truncated,
        "recruiting_marketing": is_recruiting_marketing(subject, record.body),
        "explicit_application_subject": bool(
            EXPLICIT_APPLICATION_SUBJECT.search(subject)
        ),
        "non_recruiting_subject": bool(
            NON_RECRUITING_SUBJECT.search(subject)
        ),
        "recruiting_announcement_subject": bool(
            re.search(r"(?:空宣|云宣讲|宣讲会)", subject)
        ),
        "recruiting_keyword": any(
            keyword.casefold() in combined.casefold()
            for keyword in RECRUITING_KEYWORDS
        ),
        "marketing_footer": bool(
            re.search(r"云宣讲|宣讲会", combined)
            and re.search(
                r"直通|抢先拿\s*offer|邀你赴约",
                combined,
                re.IGNORECASE,
            )
        ),
    }


def normalize_location(value: str | None) -> str | None:
    """Normalize an explicit set of job cities while preserving source order."""
    if not value:
        return None
    cities: list[str] = []
    for match in re.finditer(LOCATION_TOKEN, value):
        city = match.group(0).removesuffix("市")
        if city not in cities:
            cities.append(city)
    return " / ".join(cities) or None


def _role_metadata(
    role_raw: str | None,
) -> tuple[str | None, str | None, str | None]:
    if not role_raw:
        return None, None, None
    cleaned = normalize(role_raw).strip(" -—|：:")
    cleaned = ROLE_COHORT_PREFIX.sub("", cleaned)
    code_matches = list(ROLE_JOB_CODE.finditer(cleaned))
    job_code = code_matches[-1].group("code").upper() if code_matches else None
    without_code = ROLE_JOB_CODE_SUFFIX.sub("", cleaned).strip(" -—·_/")
    without_code = ROLE_JOB_CODE_PREFIX.sub("", without_code).strip(" -—·_/")
    base_location_match = ROLE_BASE_LOCATION_COHORT.search(without_code)
    base_location = (
        normalize_location(base_location_match.group("value"))
        if base_location_match
        else None
    )
    if base_location_match:
        without_code = (
            without_code[: base_location_match.start()]
            + without_code[base_location_match.end() :]
        ).strip(" -—·_/")
    without_code = ROLE_COHORT_SUFFIX.sub("", without_code).strip(" -—·_/")
    location_match = ROLE_LOCATION_SUFFIX.search(without_code)
    role_location = (
        base_location
        or (
            normalize_location(location_match.group("value"))
            if location_match
            else None
        )
    )
    if location_match:
        without_code = without_code[: location_match.start()].strip(
            " -—–｜|/（("
        )
    return without_code or None, role_location, job_code


def _location(
    text: str,
    role_raw: str | None,
) -> tuple[str | None, float, str | None]:
    # Only explicit job/office labels are accepted from message text. This
    # deliberately ignores interview/exam/check-in/meeting locations, Beijing
    # time, company-name cities and footer/company addresses.
    _, role_location, _ = _role_metadata(role_raw)
    if role_location:
        return role_location, 0.94, "role-location-suffix"
    label = LOCATION_LABEL.search(text)
    if label:
        value = normalize_location(label.group("value"))
        if value:
            return value, 0.98, "explicit-job-location-label"
    return None, 0.0, None


def _year(explicit: str | None, month: int, day: int, received: datetime) -> int:
    if explicit:
        return int(explicit)
    candidate = datetime(received.year, month, day, tzinfo=SHANGHAI)
    return received.year + 1 if candidate < received - timedelta(days=180) else received.year


def _dt(
    year: str | None,
    month: str,
    day: str,
    hour: str,
    minute: str,
    received: datetime,
) -> datetime:
    parsed_hour = int(hour)
    if parsed_hour == 24 and int(minute) != 0:
        raise ValueError("24 hour notation is only valid at 24:00")
    value = datetime(
        _year(year, int(month), int(day), received),
        int(month),
        int(day),
        0 if parsed_hour == 24 else parsed_hour,
        int(minute),
        tzinfo=SHANGHAI,
    )
    return value + timedelta(days=1) if parsed_hour == 24 else value


def _try_dt(
    year: str | None,
    month: str,
    day: str,
    hour: str,
    minute: str,
    received: datetime,
) -> datetime | None:
    """Ignore numeric fragments that resemble dates but are not calendar values."""
    try:
        return _dt(year, month, day, hour, minute, received)
    except ValueError:
        return None


def _chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        return 10
    if "百" in value:
        left, _, right = value.partition("百")
        return digits.get(left, 1) * 100 + (_chinese_number(right) or 0)
    if "十" in value:
        left, _, right = value.partition("十")
        return digits.get(left, 1) * 10 + (digits.get(right, 0) if right else 0)
    return digits.get(value)


def _times(
    text: str,
    received: datetime,
) -> tuple[datetime | None, datetime | None, datetime | None, int | None]:
    start = end = deadline = None
    duration_match = DURATION.search(text)
    duration_minutes = (
        int(duration_match.group("minutes"))
        if duration_match and 5 <= int(duration_match.group("minutes")) <= 480
        else None
    )
    if duration_minutes is None:
        duration_hours = DURATION_HOURS.search(text)
        if duration_hours:
            candidate_duration = int(float(duration_hours.group("hours")) * 60)
            if 5 <= candidate_duration <= 480:
                duration_minutes = candidate_duration
    full = FULL_RANGE.search(text)
    partial = PARTIAL_CROSS_RANGE.search(text) if not full else None
    same = SAME_DAY_RANGE.search(text) if not full and not partial else None
    if full:
        g = full.groupdict()
        candidate_start = _try_dt(
            g["sy"], g["sm"], g["sd"], g["sh"], g["smin"], received
        )
        candidate_end = _try_dt(
            g["ey"], g["em"], g["ed"], g["eh"], g["emin"], received
        )
        if candidate_start and candidate_end:
            start, end = candidate_start, candidate_end
    elif partial:
        g = partial.groupdict()
        candidate_start = _try_dt(
            g["sy"], g["sm"], g["sd"], g["sh"], g["smin"], received
        )
        candidate_end = _try_dt(
            g["sy"], g["em"], g["ed"], g["eh"], g["emin"], received
        )
        if candidate_start and candidate_end:
            start, end = candidate_start, candidate_end
    elif same:
        g = same.groupdict()
        candidate_start = _try_dt(
            g["y"], g["m"], g["d"], g["sh"], g["smin"], received
        )
        if candidate_start:
            start = candidate_start
            end_hour = int(g["eh"])
            end = start.replace(
                hour=0 if end_hour == 24 else end_hour,
                minute=int(g["emin"]),
            )
            if end_hour == 24:
                end += timedelta(days=1)
            if end <= start:
                end += timedelta(days=1)
    datetime_matches = [*DATETIME.finditer(text), *CN_DATETIME.finditer(text)]
    datetime_matches.sort(key=lambda item: item.start())
    for match in datetime_matches:
        g = match.groupdict()
        candidate = _try_dt(
            g["y"],
            g["m"],
            g["d"],
            g["h"],
            g.get("min") or "00",
            received,
        )
        if candidate is None:
            continue
        before = text[max(0, match.start() - 36) : match.start()]
        after = text[match.end() : match.end() + 20]
        meridiem_context = text[
            max(0, match.start() - 12) : match.end()
        ]
        if (
            re.search(r"(?:下午|晚上|傍晚)", meridiem_context)
            and candidate.hour < 12
        ):
            candidate += timedelta(hours=12)
        elif "中午" in meridiem_context and candidate.hour < 11:
            candidate += timedelta(hours=12)
        window_start_before = re.search(
            r"(?:考试|笔试|测评|作答)(?:开始|开放)时间\s*[:：]?\s*$",
            before,
        )
        window_end_before = re.search(
            r"(?:考试|笔试|测评|作答)(?:结束|截止)时间\s*[:：]?\s*$",
            before,
        )
        appointment_end_before = re.search(
            r"(?:面试|会议)结束时间\s*[:：]?\s*$",
            before,
        )
        appointment_start_before = re.search(
            r"(?:面试|会议)(?:开始)?时间\s*[:：]?\s*$",
            before,
        )
        deadline_before = re.search(
            r"(?:截止(?:时间)?|截至|最迟|有效期至|最晚|请在|须在|务必在)"
            r"[^。；;]{0,30}$",
            before,
        )
        deadline_after = re.search(
            r"^\s*(?:之前|前(?:\s|完成|[，,。；;])|失效|到期|过期)",
            after,
        )
        if window_end_before:
            deadline = candidate
        elif appointment_end_before:
            end = candidate
        elif window_start_before or appointment_start_before:
            start = candidate
        elif deadline_before or deadline_after:
            deadline = candidate
        elif re.search(r"(?:生成|发送|发出|邮件)时间\s*[:：]?\s*$", before):
            continue
        elif re.search(r"^\s*生效", after) or start is None:
            start = candidate
    if start and deadline and (
        (re.search(r"生效", text) and re.search(r"失效", text))
        or (re.search(r"邀请于", text) and re.search(r"失效", text))
    ):
        end, deadline = deadline, None
    if deadline is None:
        relative = RELATIVE_DEADLINE.search(text) or RELATIVE_EXPIRY.search(text)
        if relative:
            amount = _chinese_number(relative.group("amount")) or 0
            if 0 < amount <= 720:
                # Senders write 工作日 loosely; counting calendar days keeps the
                # reminder earlier than the real cut-off rather than later.
                delta = (
                    timedelta(hours=amount)
                    if relative.group("unit") == "小时"
                    else timedelta(days=amount)
                )
                deadline = received + delta
    if start and end is None and deadline is None and duration_minutes:
        end = start + timedelta(minutes=duration_minutes)
    return start, end, deadline, duration_minutes


def _stage(subject: str, body: str) -> str:
    combined = f"{subject} {body}"
    body_signal = re.sub(
        r"(?:招聘|应聘)?流程(?:为|是|[:：])?"
        r"[^。；;\n]{0,240}",
        "",
        body,
        flags=re.IGNORECASE,
    )
    body_signal = re.sub(
        r"(?:若|如果|如)[^。；;\n]{0,80}?(?:未通过|不通过)"
        r"[^。；;\n]*",
        "",
        body_signal,
    )
    if re.search(
        r"(?:未通过|遗憾通知|不予录用|应聘终止|流程终止)",
        subject,
    ) or re.search(
        r"遗憾.{0,40}(?:无法|不能).{0,40}(?:继续|参与|邀请)",
        body_signal,
    ) or re.search(
        r"(?:结果|状态)\s*[:：]\s*(?:未通过|不匹配|淘汰|拒绝)",
        body_signal,
    ) or (
        re.search(r"应聘结果反馈", subject)
        and bool(
            re.search(
                r"(?:未来|后续).{0,30}(?:适合|合适).{0,12}职位|"
                r"有适合您?的职位开放",
                body_signal,
            )
        )
    ):
        return "未通过"
    if re.search(r"群面|综合面", subject) or re.search(
        r"面试方式\s*[:：].{0,16}群面", body_signal
    ):
        return "群面"
    if re.search(
        r"待(?:你|您)?\s*(?:完成|提交)网申|完成网申后",
        combined,
    ):
        return "招聘通知"
    receipt_pattern = (
        r"(?:简历)?投递成功|简历(?:已经|已)收到|内推成功确认|"
        r"(?:网申|申请)成功|感谢(?:您|你)?投递|"
        r"(?:您|你)?已成功申请[^。；]{2,100}?职位|"
        r"(?:我们)?已收到(?:您|你)的申请|"
        r"(?:您|你)?已成功投递[^。；]{2,100}?职位|"
        r"已收到(?:您|你)?对[^。；]{2,100}?的申请"
    )
    if re.search(receipt_pattern, subject):
        return "网申"
    lowered_subject = subject.lower()
    for stage, keywords in STAGES:
        if any(keyword.lower() in lowered_subject for keyword in keywords):
            return stage
    if re.search(
        r"(?:邀请|诚邀|安排|进入|参加|请于|请在|完成).{0,50}"
        r"(?:在线笔试|在线考试|笔试|考试)",
        body_signal,
    ):
        return "在线笔试"
    if re.search(
        r"(?:邀请|诚邀|安排|进入|参加|请于|请在|完成).{0,50}"
        r"(?:人才测评|在线测评|AI测评|测评)",
        body_signal,
    ):
        return "人才测评"
    if re.search(r"AI\s*面试", body_signal, re.IGNORECASE):
        return "AI 面试"
    if re.search(r"(?:HR|人力)\s*面试", body_signal, re.IGNORECASE):
        return "HR 面试"
    if re.search(
        r"(?:邀请|诚邀|安排|进入|参加|请于|请在).{0,50}"
        r"(?:面试|初面|[一二三四五]面|终面)",
        body_signal,
    ):
        return "面试"
    if re.search(
        r"(?:简历评估|简历筛选).{0,12}(?:通过|完成)|"
        r"(?:通过|完成).{0,12}(?:简历评估|简历筛选)",
        body_signal,
    ):
        return "简历筛选"
    if re.search(
        r"(?:录用通知|录用意向|恭喜.{0,20}(?:录用|offer)|"
        r"(?:获得|发放|收到)\s*offer)",
        body_signal,
        re.IGNORECASE,
    ):
        return "Offer"
    if re.search(receipt_pattern, body_signal):
        return "网申"
    if re.search(
        r"(?:邀请|诚邀|安排|进入|参加|请于|请在|完成|通知(?:您|你))",
        body_signal,
    ):
        lowered_body = body_signal.lower()
        for stage, keywords in STAGES:
            if stage in {"Offer", "未通过", "网申"}:
                continue
            if any(keyword.lower() in lowered_body for keyword in keywords):
                return stage
    return "招聘通知"


def _event_type(stage: str) -> str:
    return {
        "Offer": "offer",
        "未通过": "rejection",
        "简历筛选": "application",
        "在线笔试": "assessment",
        "人才测评": "assessment",
        "AI 面试": "interview",
        "HR 面试": "interview",
        "群面": "interview",
        "面试": "interview",
        "材料截止": "deadline",
        "简历完善": "deadline",
        "网申": "application",
    }.get(stage, "notice")


def _change(subject: str, body: str) -> str:
    if re.search(r"取消|作废|无需参加", subject):
        return "cancel"
    if re.search(
        r"(?:原定|原计划).{0,30}(?:取消|作废|无需参加)|"
        r"原(?:面试|笔试|测评|考试|会议)?安排.{0,30}(?:取消|作废|无需参加)",
        body,
    ):
        return "cancel"
    if re.search(r"改期|时间调整|时间变更|更新通知", subject):
        return "update"
    return "new"


def _select_candidate(
    candidates: list[FieldCandidate],
) -> FieldSelection:
    def source_priority(source: str) -> int:
        if "subject-" in source:
            return 3
        if "body-" in source or "role-" in source:
            return 2
        if "sender" in source:
            return 1
        return 2

    grouped: dict[str, list[FieldCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.value.casefold(), []).append(candidate)
    ranked: list[FieldCandidate] = []
    for values in grouped.values():
        best = max(
            values,
            key=lambda item: (item.evidence_tier, item.confidence),
        )
        corroboration = min(0.06, 0.03 * (len({item.source for item in values}) - 1))
        ranked.append(
            FieldCandidate(
                value=best.value,
                source="+".join(sorted({item.source for item in values})),
                confidence=min(0.99, best.confidence + corroboration),
                evidence_tier=best.evidence_tier,
                raw_value=best.raw_value,
            )
        )
    ranked.sort(
        key=lambda item: (
            item.evidence_tier,
            source_priority(item.source),
            item.confidence,
        ),
        reverse=True,
    )
    if not ranked or ranked[0].confidence < 0.55:
        return FieldSelection(None, 0.0, None)
    top_tier = ranked[0].evidence_tier
    top_source_priority = source_priority(ranked[0].source)
    if (
        sum(
            item.evidence_tier == top_tier
            and source_priority(item.source) == top_source_priority
            for item in ranked
        )
        > 1
    ):
        return FieldSelection(None, 0.0, "conflicting-identity-evidence")
    selected = ranked[0]
    return FieldSelection(
        selected.value,
        selected.confidence,
        selected.source,
        selected.raw_value,
    )


def _company_candidate(
    value: str,
    source: str,
    confidence: float,
    evidence_tier: int,
    dictionaries: IdentityDictionaries | None,
) -> FieldCandidate | None:
    cleaned = normalize(value).strip("【】[]（）()，,。:： -—|")
    # Strip a recruiting suffix only from an unambiguous legal company name.
    # Do not globally rename stored applications or guess employer abbreviations.
    bare_company = strip_one_safe_company_recruitment_suffix(cleaned)
    if bare_company and re.fullmatch(LEGAL_COMPANY_NAME, bare_company):
        cleaned = bare_company
    cleaned = re.sub(
        r"(?:20)?\d{2}届?(?:校园招聘|校招|秋招|春招)$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" -—|")
    if not cleaned or COMPANY_NOISE.search(cleaned):
        return None
    if len(re.findall(r"有限公司|有限责任公司", cleaned)) > 1:
        return None
    dictionary_match = dictionaries.company_match(cleaned) if dictionaries else None
    # A reviewed label is always an employer; anything else naming a venue, a
    # document, or the applicant is not, and must not rival the real company.
    if not dictionary_match and NON_EMPLOYER_LABEL.search(cleaned):
        return None
    company = dictionary_match.canonical if dictionary_match else canonical_company(cleaned)
    if not company:
        return None
    reviewed_source = None
    if dictionary_match:
        reviewed_source = dictionary_match.match_type
    elif company != cleaned:
        reviewed_source = "built-in-exact-alias"
        if dictionaries:
            company = dictionaries.canonical_company(company) or company
    if reviewed_source:
        confidence = min(0.99, confidence + 0.04)
        evidence_tier = min(4, evidence_tier + 1)
        source = f"{source}+reviewed-{reviewed_source}"
    if not 2 <= len(company) <= 50:
        return None
    return FieldCandidate(company, source, confidence, evidence_tier)


def _company(
    subject: str,
    body: str,
    sender: str,
    dictionaries: IdentityDictionaries | None = None,
) -> tuple[str | None, float, str | None]:
    candidates: list[FieldCandidate] = []

    def add(
        match: re.Match[str] | None,
        source: str,
        confidence: float,
        evidence_tier: int = 3,
    ) -> None:
        if not match:
            return
        candidate = _company_candidate(
            match.group(1),
            source,
            confidence,
            evidence_tier,
            dictionaries,
        )
        if candidate:
            candidates.append(candidate)

    for bracket_match in re.finditer(r"【([^】]{2,30})】", subject):
        add(bracket_match, "subject-brackets", 0.96)
    add(SUBJECT_EXAM_COMPANY_ROLE.search(subject), "subject-exam-employer", 0.98, 4)
    add(SUBJECT_LEGAL_COMPANY_STAGE.search(subject), "subject-legal-employer", 0.98, 4)
    add(re.match(r"来自([^的]{2,30})的", subject), "subject-from-label", 0.98)
    add(
        re.search(r"请尽快完成([^，。]{2,50}?)发起的(?:AI)?(?:面试|测评)", subject),
        "subject-launched-by",
        0.94,
    )
    add(
        re.search(
            r"^(?!【|来自|请|邀请|感谢|欢迎|尊敬)"
            r"([^，。]{2,50}?)(?:视频)?面试(?:邀约|邀请|通知)", subject,
        ),
        "subject-interview-prefix",
        0.91,
    )
    structured_subject = SUBJECT_APPLICATION_COMPANY_ROLE.search(subject)
    if structured_subject:
        candidate = _company_candidate(
            structured_subject.group("company"),
            "subject-application-company",
            0.98,
            4,
            dictionaries,
        )
        if candidate:
            candidates.append(candidate)
    else:
        add(
            re.search(
                r"感谢(?:您|你)投递"
                r"([^，。！!]{2,50}?)(?:校园招聘|校招|招聘|职位|岗位|[！!。]|$)",
                subject,
            ),
            "subject-acknowledgement",
            0.88,
        )
    add(
        re.search(
            r"(?:公司名称|应聘公司|招聘公司|招聘单位|用人单位|应聘单位|雇主)\s*[:：]\s*"
            r"([^\n，。；;]{2,50})",
            body,
        ),
        "body-company-label",
        0.98,
    )
    add(
        re.search(
            r"感谢(?:您|你)?投递\s*([^，。\n]{2,50}?)(?:公司)?的"
            r"[^，。\n]{2,100}?(?:职位|岗位)",
            body,
        ),
        "body-application-company",
        0.96,
    )
    add(
        re.search(
            r"(?:请尽快完成|邀请(?:您|你)?参加)"
            r"([^，。\n]{2,50}?)发起的(?:AI)?(?:面试|测评|笔试)",
            body,
        ),
        "body-launched-by",
        0.94,
    )

    sender_name, sender_address = parseaddr(sender)
    sender_confidence = (
        0.72
        if re.search(r"(?:^|[_\-\s])(hr|noreply|no-reply|recruit)", sender_name, re.I)
        else 0.9
    )
    sender_candidate = _company_candidate(
        sender_name,
        "sender-display-name",
        sender_confidence,
        2,
        dictionaries,
    )
    # A reviewed label is an employer even when it is short, so only an
    # unrecognised display name is judged by shape.
    if sender_candidate and (
        "+reviewed-" in sender_candidate.source
        or not _is_personal_sender_name(sender_name)
    ):
        candidates.append(sender_candidate)
    if dictionaries and "@" in sender_address:
        domain_company = dictionaries.company_for_email_domain(
            sender_address.rsplit("@", 1)[1]
        )
        if domain_company:
            candidates.append(
                FieldCandidate(
                    domain_company,
                    "reviewed-sender-domain",
                    0.95,
                    3,
                )
            )
    # This deliberately remains weak. Marketing copy often contains phrases
    # such as “快来 Get 最新招聘信息”, which must never outrank sender/template
    # evidence.
    add(
        re.search(
            r"(?:^|[，。；;！!])\s*"
            r"([\u4e00-\u9fffA-Za-z·.]{2,30}?)(?:20\d{2})?"
            r"(?:校园招聘|校招|招聘|邀请|笔试|测评|面试)",
            subject,
        ),
        "weak-subject-prefix",
        0.58,
        1,
    )
    # Last resort: the employer is often named only in the prose while the
    # From header carries an HR's personal name or an ATS product name.
    if dictionaries:
        for text, source, confidence in (
            (subject, "reviewed-subject-mention", 0.93),
            (body, "reviewed-body-mention", 0.88),
        ):
            mention = dictionaries.find_company_mention(text)
            if not mention:
                continue
            # "本中茵微电子" and "阿里巴巴淘天集团" name the same firms as the
            # reviewed labels inside them. Treat those as agreement and keep
            # the canonical form rather than reporting a conflict.
            candidates = [
                candidate
                for candidate in candidates
                if candidate.value == mention.canonical
                or (
                    mention.canonical not in candidate.value
                    and candidate.value not in mention.canonical
                )
            ]
            candidates.append(
                FieldCandidate(mention.canonical, source, confidence, 3)
            )
            break

    selected = _select_candidate(candidates)
    return selected.value, selected.confidence, selected.source


def _clean_role(
    value: str,
    dictionaries: IdentityDictionaries | None,
) -> tuple[str, str, bool] | None:
    cleaned = normalize(value)
    cleaned = re.split(
        r"(?:\s*(?:面试时间|笔试时间|测评时间)\s*[:：，,]|"
        r"\s+(?:结果|姓名|候选人)\s*[:：])",
        cleaned,
        maxsplit=1,
    )[0]
    role_raw = redact_text(cleaned).strip(" -—|：:")
    metadata_input = re.sub(
        r"^【[^】]{2,30}】\s*",
        "",
        role_raw,
    )
    metadata_input = re.sub(
        r"\s*(?:职位|岗位)(?:的)?$",
        "",
        metadata_input,
    )
    role_base, _, _ = _role_metadata(metadata_input)
    role_base = re.sub(r"(?:岗位|职位)的$", "", role_base or "").strip()
    role_base = re.sub(r"^【[^】]{2,30}】\s*", "", role_base or "")
    role_base = re.sub(r"^[】\]）)]+", "", role_base).strip()
    role = canonical_role(role_base)
    if not role or is_invalid_role(role) or ROLE_NOISE.search(role):
        return None
    if re.fullmatch(r"的?(?:AI|在线|线上|人才)?(?:考试|笔试|面试|测评)?", role, re.I):
        return None
    reviewed = dictionaries.canonical_role(role) if dictionaries else None
    return role_raw[:120], (reviewed or role)[:80], bool(reviewed)


def _role(
    subject: str,
    body: str,
    dictionaries: IdentityDictionaries | None = None,
) -> FieldSelection:
    text = f"{subject}\n{body}"
    candidates: list[FieldCandidate] = []
    structured_subject = SUBJECT_APPLICATION_COMPANY_ROLE.search(subject)
    if structured_subject:
        cleaned = _clean_role(structured_subject.group("role"), dictionaries)
        if cleaned:
            role_raw, value, reviewed = cleaned
            source = "subject-application-role"
            if reviewed:
                source += "+reviewed-alias"
            candidates.append(
                FieldCandidate(
                    value,
                    source,
                    0.99 if reviewed else 0.98,
                    4,
                    role_raw,
                )
            )
    for pattern, source_name, source_text in (
        (SUBJECT_COHORT_ROLE_STAGE, "subject-cohort-role-stage", subject),
        (SUBJECT_EXAM_COMPANY_ROLE, "subject-exam-role", subject),
        (ROLE_ASSESSMENT_APPLICATION, "body-assessment-role", body),
        (ROLE_QUOTED_SCREENING, "body-screening-role", body),
    ):
        for match in pattern.finditer(source_text):
            cleaned = _clean_role(match.group("role"), dictionaries)
            if not cleaned:
                continue
            role_raw, value, reviewed = cleaned
            if pattern is ROLE_QUOTED_SCREENING and not reviewed and not ROLE_TITLE_HINT.search(value):
                continue
            source = source_name + ("+reviewed-alias" if reviewed else "")
            candidates.append(
                FieldCandidate(
                    value,
                    source,
                    0.99 if reviewed else 0.98,
                    4 if pattern is ROLE_QUOTED_SCREENING else 5,
                    role_raw,
                )
            )
    for match in ROLE_BEFORE_COHORT_STAGE.finditer(text):
        cleaned = _clean_role(match.group("role"), dictionaries)
        if cleaned:
            role_raw, value, reviewed = cleaned
            source = "role-before-cohort-stage"
            if reviewed:
                source += "+reviewed-alias"
            candidates.append(
                FieldCandidate(value, source, 0.99 if reviewed else 0.98, 4, role_raw)
            )
    for pattern, source_name in (
        (ROLE_AFTER_COHORT_STAGE, "role-after-cohort-stage"),
        (ROLE_COHORT_TO_STAGE, "role-cohort-to-stage"),
    ):
        for match in pattern.finditer(text):
            cleaned = _clean_role(match.group("role"), dictionaries)
            if cleaned:
                role_raw, value, reviewed = cleaned
                if not value or value in {"在线", "线上", "人才", "AI", "考试"}:
                    continue
                source = source_name
                if reviewed:
                    source += "+reviewed-alias"
                candidates.append(
                    FieldCandidate(
                        value,
                        source,
                        0.99 if reviewed else 0.98,
                        4,
                        role_raw,
                    )
                )
    for index, pattern in enumerate(ROLE_PATTERNS):
        for match in pattern.finditer(text):
            cleaned = _clean_role(match.group(1), dictionaries)
            if not cleaned:
                continue
            role_raw, value, reviewed = cleaned
            if not value or value in {"在线", "线上", "人才", "AI", "考试"}:
                continue
            confidence = (
                0.98
                if index == 0
                else 0.96
                if "已收到" in match.group(0) and "申请" in match.group(0)
                else 0.9
            )
            source = f"role-pattern-{index + 1}"
            if reviewed:
                confidence = min(0.99, confidence + 0.03)
                source += "+reviewed-alias"
            candidates.append(
                FieldCandidate(
                    value,
                    source,
                    confidence,
                    4 if index == 0 else 3,
                    role_raw,
                )
            )
    return _select_candidate(candidates)


def _project(text: str) -> str | None:
    if re.search(r"长鑫存储校园招聘\s*[-·]?\s*AI初试|【[^】]*AI初试】", text):
        return "长鑫存储校园招聘"
    cycle_match = re.search(
        r"(?P<year>20\d{2})届?[^，。；;\n]{0,12}?"
        r"(?P<kind>校园招聘|校招|秋招|春招)",
        text,
    )
    cycle = None
    if cycle_match:
        kind = cycle_match.group("kind")
        cycle = (
            f"{cycle_match.group('year')}{kind}"
            if kind in {"秋招", "春招"}
            else f"{cycle_match.group('year')}校园招聘"
        )
    if not cycle:
        short_cycle = re.search(
            r"(?<!\d)(?P<year>\d{2})届"
            r"(?P<kind>校园招聘|校招|秋招|春招)",
            text,
        )
        if short_cycle:
            kind = short_cycle.group("kind")
            cycle = (
                f"20{short_cycle.group('year')}{kind}"
                if kind in {"秋招", "春招"}
                else f"20{short_cycle.group('year')}校园招聘"
            )
    business_unit = None
    if re.search(r"(?:网易游戏)?雷火(?:事业群|校招)?", text):
        business_unit = "雷火事业群"
    elif re.search(r"(?:网易游戏)?互娱(?:事业群|校招)?", text):
        business_unit = "互娱事业群"
    # Mail footers often list all JD programmes (JDS/TET/TGT). The first
    # programme mention belongs to the subject/main content; later mentions
    # are explanatory noise and must not relabel a TET task as JDS.
    jd_program_match = re.search(
        r"(?<![A-Za-z])(?P<program>JDS|TET)(?![A-Za-z])",
        text,
        re.IGNORECASE,
    )
    jd_program = jd_program_match.group("program").upper() if jd_program_match else None
    if jd_program and not cycle:
        jd_year = re.search(
            rf"(?P<year>20\d{{2}})\s*{jd_program}",
            text,
            re.IGNORECASE,
        )
        if jd_year:
            cycle = f"{jd_year.group('year')}校园招聘"
    if jd_program == "JDS" and cycle:
        return f"JDS · {cycle}"
    if jd_program == "JDS":
        return "JDS"
    if business_unit and cycle:
        return f"{business_unit} · {cycle}"
    project = business_unit or cycle
    if project and "提前批" in text:
        return f"{project} · 提前批"
    if project and "正式批" in text:
        return f"{project} · 正式批"
    if not project and "提前批" in text:
        return "提前批"
    if not project and "正式批" in text:
        return "正式批"
    return project


def _round(subject: str, body: str) -> str | None:
    chinese_numbers = {
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
    }

    def explicit(text: str) -> str | None:
        match = re.search(r"第\s*([一二三四五六\d]+)\s*轮", text)
        if match:
            value = chinese_numbers.get(match.group(1), match.group(1))
            return f"第{value}轮"
        if "综合面" in text:
            return "群面"
        for label in (
            "HRBP面试",
            "HRBP 面试",
            "技术面",
            "群面",
            "终面",
            "五面",
            "四面",
            "三面",
            "二面",
            "一面",
            "HR面",
            "HR 面",
        ):
            if label in text:
                return label.replace(" ", "")
        return None

    subject_round = explicit(subject)
    if subject_round:
        return subject_round
    contextual = re.search(
        r"(?:本次|本轮|当前|进入|参加|安排).{0,16}?"
        r"(HRBP\s*面试|技术面|群面|终面|[一二三四五]面|第\s*[一二三四五六\d]+\s*轮)",
        body,
    )
    return explicit(contextual.group(0)) if contextual else (
        "AI初试" if "AI初试" in f"{subject} {body}" else None
    )


def _evidence(text: str) -> tuple[str, ...]:
    sentences = re.split(r"(?<=[。！？!?；;])\s*", text)
    selected: list[str] = []
    for sentence in sentences:
        if any(
            keyword in sentence
            for keyword in (
                "请于",
                "请在",
                "时间",
                "截止",
                "失效",
                "准备",
                "携带",
                "完成",
                "链接",
                "面试",
                "笔试",
                "测评",
                "简历",
            )
        ):
            cleaned = redact_text(sentence)
            cleaned = re.sub(r"https?://\S+", "[本地链接]", cleaned)
            if 8 <= len(cleaned) <= 220 and cleaned not in selected:
                selected.append(cleaned)
        if len(selected) >= 3:
            break
    return tuple(selected)


def _action_link(record: MailRecord, combined: str) -> str | None:
    candidates = [*record.links, *URL_PATTERN.findall(combined)]
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, raw in enumerate(candidates):
        value = raw.rstrip(".,;，。；)>】")
        if value in seen:
            continue
        seen.add(value)
        parsed = urlsplit(value)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            continue
        lowered = value.casefold()
        if re.search(
            r"unsubscribe|optout|privacy|preference|tracking|退订|取消订阅",
            lowered,
        ):
            continue
        score = 0
        if re.search(
            r"exam|test|written|interview|assessment|meeting|showmebug|"
            r"nowcoder|zoom|voov|tencent",
            lowered,
        ):
            score += 5
        position = combined.find(value)
        if position >= 0:
            context = combined[max(0, position - 24) : position + len(value) + 12]
            if re.search(r"考试|笔试|测评|面试|作答|会议|进入|参加|链接|地址", context):
                score += 6
            if re.search(r"官网|隐私|退订|帮助中心", context):
                score -= 6
        ranked.append((score, -order, value))
    return max(ranked)[2] if ranked else None


def parse_record(
    record: MailRecord,
    dictionaries: IdentityDictionaries | None = None,
    *,
    include_onsite_sessions: bool = False,
) -> ParsedEvent | None:
    subject = normalize(record.subject)
    body = normalize(record.body)
    structured_body = normalize_multiline(record.body)
    combined = normalize(f"{subject} {body}")
    explicit_application_subject = bool(
        EXPLICIT_APPLICATION_SUBJECT.search(subject)
    )
    if NON_RECRUITING_SUBJECT.search(subject):
        return None
    onsite_announcement = False
    if is_recruiting_marketing(subject, body):
        onsite_announcement = bool(
            include_onsite_sessions
            and re.search(r"宣讲", combined)
            and ONSITE_SESSION_VENUE.search(structured_body)
            and _times(
                unicodedata.normalize("NFKC", combined),
                record.received_at.astimezone(SHANGHAI),
            )[0] is not None
        )
        if not onsite_announcement:
            return None
    matches = tuple(
        keyword
        for keyword in RECRUITING_KEYWORDS
        if keyword.lower() in combined.lower()
    )
    subject_matches = tuple(
        keyword
        for keyword in RECRUITING_KEYWORDS
        if keyword.lower() in subject.lower()
    )
    if not matches and explicit_application_subject:
        matches = ("明确投递标题",)
    if not matches and onsite_announcement:
        matches = ("线下宣讲会",)
    if not matches:
        return None
    if (
        not subject_matches
        and not explicit_application_subject
        and not onsite_announcement
        and not re.search(
            r"邀请|诚邀|申请|投递|请于|请在|须在|务必在|截止|"
            r"安排|进入|完成|未通过|不匹配|录用|"
            r"岗位名称|应聘岗位|职位名称|工作地点|岗位地点|"
            r"(?:测评|考试|面试|笔试)(?:链接|时间|邀请|安排|通知)",
            body,
            re.IGNORECASE,
        )
    ):
        return None
    # Public "直通 offer / 现场面试" advertising is still a campus event,
    # not an offer or a personal interview assignment.
    stage = "招聘通知" if onsite_announcement else _stage(subject, body)
    start, end, deadline, duration_minutes = _times(
        unicodedata.normalize("NFKC", combined),
        record.received_at.astimezone(SHANGHAI),
    )
    company, company_confidence, company_source = _company(
        subject,
        structured_body,
        record.sender,
        dictionaries,
    )
    role_selection = _role(
        subject,
        structured_body,
        dictionaries,
    )
    role = role_selection.value
    role_raw = role_selection.raw_value
    role_confidence = role_selection.confidence
    role_source = role_selection.source
    if not role and re.search(r"TET\s*综合(?:面|方向)", subject, re.IGNORECASE):
        role = "TET 综合方向"
        role_raw = "TET 综合方向"
        role_confidence = 0.9
        role_source = "subject-tet-direction"
    _, _, job_code = _role_metadata(role_raw or role)
    location, location_confidence, location_source = _location(
        f"{subject}\n{structured_body}",
        role_raw or role,
    )
    project = _project(combined)
    if company == "网易招聘" and project and re.search(r"雷火事业群|互娱事业群", project):
        company = "网易游戏"
    requirements = _evidence(record.body)
    source_id = record.message_id or (
        "<generated-"
        + sha256(
            f"{record.sender}|{subject}|{record.received_at.isoformat()}".encode()
        ).hexdigest()
        + ">"
    )
    source_url = _action_link(record, combined)
    confidence = 0.45
    confidence += min(0.2, len(matches) * 0.04)
    confidence += 0.2 if start or deadline else 0
    confidence += 0.08 if company else 0
    confidence += 0.04 if role else 0
    confidence += 0.03 if stage != "招聘通知" else 0
    if stage == "未通过":
        action = "确认招聘结果，并决定是否归档本次申请。"
        requirements = ()
    elif stage == "Offer":
        action = "核对 Offer 内容、回复要求和明确截止时间。"
    elif stage == "群面":
        identity = " ".join(item for item in (company, role) if item)
        action = f"参加{identity}群面；提前准备并核对会议入口。"
    else:
        action = requirements[0] if requirements else redact_text(subject)
    event_type = _event_type(stage)
    return ParsedEvent(
        company=company,
        role=role,
        recruiting_project=project,
        event_type=event_type,
        stage=stage,
        round=None if event_type == "application" else _round(subject, body),
        title=redact_text(subject),
        start_at=start,
        end_at=end,
        deadline_at=deadline,
        source_message_id=source_id,
        source_received_at=record.received_at.astimezone(SHANGHAI),
        source_sender=record.sender,
        source_url=source_url,
        action_summary=action,
        requirements=requirements,
        matched_keywords=matches,
        confidence=round(min(0.99, confidence), 2),
        change_type=_change(subject, body),  # type: ignore[arg-type]
        location=location,
        location_confidence=location_confidence,
        location_source=location_source,
        company_confidence=company_confidence,
        company_source=company_source,
        role_confidence=role_confidence,
        role_source=role_source,
        duration_minutes=duration_minutes,
        role_raw=role_raw,
        role_canonical=role,
        job_code=job_code,
    )
