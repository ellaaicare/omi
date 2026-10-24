"""Closed fictional test comparator, not a general semantic safety classifier.

Prospective script + mandatory facts + a fresh spoken challenge. Finite ASR
aliases/function-word omissions are tolerated; extra content, contradictions and
missing facts fail closed. No model call or source normalization/write occurs.
"""
import re
import unicodedata

CONTRACT = 'ella-maple-workshop-anchored-asr-v1'
BASE = ('This is a fictional planning conversation about the Maple Harbor museum workshop. '
    'Mira and Nolan decided to use reusable paper lanterns instead of disposable plastic decorations '
    'because they want less waste. The workshop is Saturday, October seventeenth, at ten in the morning. '
    'Mira will prepare twelve blue lantern kits and bring the printed instructions. '
    'Nolan will check the room booking and confirm the supply list. '
    'Remind Mira on Thursday, October fifteenth, at three in the afternoon Pacific time '
    'to finish the lantern kits before the workshop.')
MARKER_WORDS = {'orange','silver','green','purple','river','meadow','garden','valley',
    'ticket','window','ribbon','basket','copper','yellow','island','forest'}
NUMBERS = {'one':'1','two':'2','three':'3','four':'4','five':'5','six':'6','seven':'7',
    'eight':'8','nine':'9','ten':'10','twelve':'12','fifteenth':'15','seventeenth':'17'}
ALIASES = {'myra':'mira','harbour':'harbor','lantern':'lanterns','kit':'kits',
    'instruction':'instructions','decoration':'decorations','printed':'print'}
FUNCTIONS = {'this','is','a','the','about','and','to','of','because','they','will','on','at','in','it','time'}
FILLERS = {'um','uh'}
FACTS = (
    ('fictional','planning','conversation'),
    ('maple','harbor','museum','workshop'),
    ('reusable','paper','lanterns','instead','disposable','plastic','decorations'),
    ('less','waste'),
    ('workshop','saturday','october','17','10','am'),
    ('mira','prepare','12','blue','lanterns','kits','bring','print','instructions'),
    ('nolan','check','room','booking','confirm','supply','list'),
    ('remind','mira','thursday','october','15','3','pm','pacific','finish','lanterns','kits','before','workshop'),
)

def tokens(text):
    if type(text) is not str or not 0 < len(text.encode()) <= 16000:
        raise ValueError('fixture_match_text_bound')
    s = unicodedata.normalize('NFC',text).casefold()
    for phrase,value in [('in the morning','am'),('in the afternoon','pm'),
                         ('a.m.','am'),('p.m.','pm'),('a m','am'),('p m','pm')]:
        s = re.sub(r'(?<!\w)'+re.escape(phrase)+r'(?!\w)',value,s)
    raw = re.findall(r"[^\W_]+(?:['’][^\W_]+)*",s)
    if not 0 < len(raw) <= 2048:
        raise ValueError('fixture_match_token_bound')
    return [NUMBERS.get(ALIASES.get(w,w),ALIASES.get(w,w)) for w in raw]

def challenge_shape(challenge):
    return (type(challenge) is list and len(challenge) == 4
        and len(set(challenge[:3])) == 3 and all(w in MARKER_WORDS for w in challenge[:3])
        and challenge[3] in {str(i) for i in range(1,10)})

def text_for(challenge):
    if not challenge_shape(challenge):
        raise ValueError('fixture_challenge_invalid')
    words = {v:k for k,v in NUMBERS.items()}
    return BASE + ' The check phrase is ' + ' '.join(challenge[:3]+[words[challenge[3]]]) + '.'

def _subsequence(needle,haystack):
    at = 0
    for value in haystack:
        if at < len(needle) and value == needle[at]:
            at += 1
    return at == len(needle)

def assess(text, challenge):
    """Return bounded facts/diagnostics only, never rewrite or certify ASR text.

    Exact recognized content order after closed alias normalization is required.
    Function-word omissions are tolerated. ALL fact clauses are mandatory and
    contiguous after function-word removal. No unapproved content word can hide
    inside a numeric edit-distance threshold.
    """
    if not challenge_shape(challenge):
        return {'accepted':False,'reason':'challenge_invalid'}
    try:
        heard = tokens(text)
        expected = tokens(text_for(challenge))
    except ValueError:
        return {'accepted':False,'reason':'text_invalid'}
    content = [w for w in heard if w not in FUNCTIONS | FILLERS]
    expected_content = [w for w in expected if w not in FUNCTIONS]
    if any(w in {'not','never','no','cancel','cancelled','canceled','dont',"don't"} for w in heard):
        return {'accepted':False,'reason':'contradiction_or_negation'}
    if not _subsequence(content,expected_content):
        return {'accepted':False,'reason':'extra_reordered_or_substituted_content'}
    joined = ' ' + ' '.join(content) + ' '
    missing = [i for i,fact in enumerate(FACTS) if ' '+' '.join(fact)+' ' not in joined]
    marker = ('check','phrase',*challenge)
    if joined.count(' '+' '.join(marker)+' ') != 1:
        return {'accepted':False,'reason':'fresh_challenge_missing_or_duplicate'}
    if missing:
        return {'accepted':False,'reason':'missing_required_facts','missing_fact_indices':missing}
    # Tolerance covers function words and punctuation, not missing substantive
    # statements. This ratio is explicit and secondary to every fact above.
    coverage = len(content) / len(expected_content)
    if coverage < .90:
        return {'accepted':False,'reason':'insufficient_full_script_coverage','coverage':coverage}
    return {'accepted':True,'reason':'all_facts_fresh_challenge_and_full_content_order',
        'coverage':coverage,'recognized_tokens':len(heard),'content_tokens':len(content),
        'comparison_contract':CONTRACT,'source_text_preserved':True}
