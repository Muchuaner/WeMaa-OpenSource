"""Ordered OCR name matching; never used for message bodies or account IDs."""
import re

def normalized(value):
    return re.sub(r'\s+', '', str(value or ''))

def ordered_score(left, right):
    a,b=normalized(left),normalized(right)
    if not a or not b:return 0.0
    previous=[0]*(len(b)+1)
    for char in a:
        current=[0]
        for j,other in enumerate(b,1):
            current.append(previous[j-1]+1 if char==other else max(previous[j],current[-1]))
        previous=current
    return previous[-1]/max(len(a),len(b))

def names_match(left,right):
    a,b=normalized(left),normalized(right)
    if not a or not b:return False
    # Short names carry too little evidence: one OCR misread or a shared
    # two-character prefix must never merge two distinct contacts.
    if min(len(a),len(b))<=4:return a==b
    return ordered_score(a,b)>=0.7

def names_confusable(left,right):
    """Loose similarity for ambiguity guards only; never accepts a match.

    Kept deliberately looser than names_match: clipping a sidebar name to a
    prefix or misreading one short-name character must still abort navigation
    when another visible row could be the same contact.
    """
    a,b=normalized(left),normalized(right)
    if not a or not b:return False
    if ordered_score(a,b)>=0.7:return True
    shorter,longer=sorted((re.sub(r'(?:\.{3}|…+)$','',a),re.sub(r'(?:\.{3}|…+)$','',b)),key=len)
    return len(shorter)>=8 and longer.startswith(shorter)

def group_header(row_name,header):
    """Count must be additional UI text, not part of a literal private nickname."""
    name,title=normalized(row_name),normalized(header)
    match=re.fullmatch(r'(.+)[（(]([1-9]\d{0,3})[）)]?',title)
    if not match or name==title or not names_match(name,match[1]):return None
    return match[1],int(match[2])

def title_base(value):
    return re.sub(r'[（(]\d{1,4}[）)]?$','',normalized(value))
