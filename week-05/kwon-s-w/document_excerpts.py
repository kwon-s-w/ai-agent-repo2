"""Select bounded excerpts for the free-tier model without sending arbitrary page text."""
import re

DATE = r'(?:19|20)\d{2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2}\.?'
MONEY = r'\d{1,3}(?:,\d{3}){1,4}(?:\s*원)?'
AREA = r'\d{1,5}(?:\.\d{1,4})?\s*(?:㎡|m²|제곱미터)'
USE = r'(?:판매시설|업무시설|근린생활시설|아파트|연립주택|다세대주택|공동주택|주택|오픈형상가)'

# Each pattern emits fixed words and typed values. Unmatched lines stay local.
PATTERNS = tuple(re.compile(pattern) for pattern in (
    rf'(?:최선순위\s*)?설정(?:일자)?\s*{DATE}\s*(?:근저당권|저당권)(?:\s*배당요구종기\s*{DATE})?',
    rf'(?:최선순위\s*)?(?:근저당권|저당권)\s*(?:설정(?:일자)?\s*)?{DATE}',
    rf'전세권\s*등기일(?:은|\s*[:：])?\s*{DATE}',
    rf'주거\s*전세\s*권자\s+{DATE}\s*[~～-]\s*{DATE}\s+{MONEY}',
    r'2\s*분의\s*1\s*소유자',
    rf'배당요구종기\s*{DATE}',
    rf'권리신고\s*(?:(?:점포|상가|주택)\s*)?임차인\s*(?:{DATE}\s*[~～-]?\s*)?{MONEY}\s+{MONEY}(?:\s+{DATE}){{1,3}}',
    rf'임차인\s+{DATE}(?:\s+{DATE})?',
    rf'(?:전입상세내용|등기부등본확정내용|확정일자|배당요구일자|사업자등록신청일자)\s+{DATE}',
    r'점유자\s*등으로부터\s*아무\s*연락이\s*없어\s*상가건물\s*임대차현황서에\s*등재된\s*점유자를\s*일응\s*임차인으로\s*보고함\.?',
    r'상가건물임대차현황서\s*내역에\s*임차인만\s*등재되어\s*있으며',
    r'(?:폐문부재|점유자\s*미상|점유관계\s*미상|점유\s*확인\s*불가)',
    rf'감정평가액\s*[:：]?\s*[￦₩]?\s*{MONEY}',
    rf'(?:감정평가\s*)?기준시점\s*[:：]?\s*{DATE}',
    r'내부\s*확인이\s*불가능',
    r'소재\s*필지\s*및\s*관리주체\s*등이\s*상이',
    rf'최저매각가(?:격)?\s*[:：]?\s*[￦₩]?\s*{MONEY}',
    rf'\d{{1,2}}회\s+{DATE}\s+{MONEY}\s+{MONEY}',
    rf'(?:전유면적|전용면적)\s*(?:은\s*)?[:：]?\s*{AREA}',
    rf'(?<![\d.]){AREA}(?![\d.])',
    rf'용\s*도\s*[:：]?\s*{USE}(?:\s*사용\s*승인일자\s*{DATE})?',
    r'판매시설\s*\(오픈형상가\)',
    r'본건까지\s*차량접근이\s*가능하며\s*인근에\s*버스정류장\s*및\s*지하철역',
    r'전입신고일자[·ㆍ]?\s*외국인등록\(체\s*류지변경신고\)일자[·ㆍ]?\s*사업자등\s*록\s*신청일자',
    r'\b가\s+\d{1,5}\.\d{1,4}\s+\d{1,5}\.\d{1,4}\s+\d{1,5}\.\d{1,4}\s+\d{1,5}\.\d{1,4}\s+\d{1,3}%',
    # Appraisal summary tables usually show area, price and unit price in three columns.
    rf'(?<![\d.,])\d{{1,5}}\.\d{{1,4}}[ \t]+{MONEY}[ \t]+{MONEY}',
))


def safe_excerpts(text):
    matches = sorted((match.start(), match.end(), match.group()) for pattern in PATTERNS
                     for match in pattern.finditer(text))
    lines = []
    for _, _, matched in matches:
        line = ' '.join(matched.split())
        if line not in lines:
            lines.append(line)
    return '\n'.join(lines)
