"""Pure fresh-admission title policy. No truncation, I/O, or public schema field."""
import re

TITLE_REVISION_PREFIX = 'concise-complete-title-v1:'
MAX_TITLE_CHARACTERS = 100
MAX_TITLE_WORDS = 10

TITLE_GENERATOR_RULES = '''Write one concise, complete Journal title in the source
language. Prefer a specific natural phrase of 2-6 words; use up to 10 words and
100 characters when needed to preserve the subject or an important distinction.
Keep questions, proposals and decisions distinct. Do not imply a decision when
the source contains only a question or suggestion. Omit generic wrappers such
as "Conversation about" or "Discussion of". Use only source-supported details.
Return the entire title as plain text, without markdown, line breaks or an
ellipsis. Compose a shorter complete phrase; never cut a sentence or keep only
its first four words. Brevity must not remove a condition or change meaning.'''

TITLE_VERIFIER_RULES = '''The title should be a concise, complete, source-grounded
phrase, not a clipped prefix. Check its assertions under the existing claim and
semantic-synthesis checks. Do not accept invented specificity or a proposed
action presented as a completed decision merely to obtain a short title.'''


def fresh_title_revision(base_revision):
    if not isinstance(base_revision, str) or not re.fullmatch('[0-9a-f]{64}', base_revision):
        raise ValueError('title_policy_revision_invalid')
    return TITLE_REVISION_PREFIX + base_revision


def has_title_quality(revision):
    return isinstance(revision, str) and bool(re.fullmatch(
        re.escape(TITLE_REVISION_PREFIX) + '[0-9a-f]{64}', revision))


def validate_complete_title(title):
    # Structural bounds only. Semantic completeness/grounding remains the
    # independent verifier's responsibility; never synthesize or trim a title.
    if (not isinstance(title, str) or not title or title != title.strip()
            or len(title) > MAX_TITLE_CHARACTERS or len(title.split()) > MAX_TITLE_WORDS
            or any(ord(c) < 32 for c in title) or title.endswith(('...', '…'))
            or '`' in title):
        raise ValueError('concise_complete_title_invalid')
    return title
