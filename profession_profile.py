"""Character abilities are user declarations; missing skill/recipe is unknown."""
from common import AppError

PRIMARY = ('tailoring', 'enchanting', 'alchemy', 'blacksmithing', 'engineering',
           'leatherworking', 'mining', 'herbalism', 'skinning')
SECONDARY = ('cooking', 'first-aid', 'fishing')
DEFAULT_FISH = [13755, 13759, 13760, 21153, 21071, 6289]
CLOTH = [2589, 2592, 4306, 4338, 14047, 14256]


def character_profile(cfg):
    fish = cfg.get('fishing_watch_items', DEFAULT_FISH)
    if not isinstance(fish, list) or any(type(i) is not int or i < 1 for i in fish):
        raise AppError('fishing_watch_items는 후보 item ID 목록입니다.')
    raw = cfg.get('character_professions')
    if raw is None:
        return {'configured': False, 'primary': {}, 'secondary': {}, 'level': None,
                'fishing_watch_items': fish}
    if not isinstance(raw, dict):
        raise AppError('character_professions는 primary/secondary 객체여야 합니다.')
    groups = {}
    for group, allowed in [('primary', PRIMARY), ('secondary', SECONDARY)]:
        values = raw.get(group, {})
        if not isinstance(values, dict) or any(p not in allowed for p in values):
            raise AppError('지원하지 않는 전문기술 분류입니다: '+group)
        if any(v is not None and (type(v) is not int or v < 0) for v in values.values()):
            raise AppError('전문기술 숙련은 0 이상 정수 또는 null(미확인)입니다.')
        groups[group] = values
    if len(groups['primary']) > 2:
        raise AppError('주 전문기술은 최대 2개입니다. 보조 전문기술은 secondary에 넣으세요.')
    level = cfg.get('character_level')
    if level is not None and (type(level) is not int or level < 1):
        raise AppError('character_level은 양의 정수 또는 null입니다.')
    return dict(configured=True, level=level, fishing_watch_items=fish, **groups)


def access(craft, profile):
    """Filtering does not establish that the character learned a spell."""
    if not profile['configured']:
        return 'profile_unknown'
    owned = dict(profile['primary'], **profile['secondary'])
    prof = craft.get('profession')
    if prof not in owned:
        return 'not_selected'
    skill, required = owned[prof], craft.get('skill')
    if skill is not None and required is not None and skill < required:
        return 'skill_too_low'
    if (profile['level'] is not None and craft.get('required_level') is not None
            and profile['level'] < craft['required_level']):
        return 'level_too_low'
    return 'requirements_check' if skill is None or required is None or not craft.get('known') else 'listed'


def allowed(craft, profile):
    return access(craft, profile) not in ('not_selected', 'skill_too_low', 'level_too_low')
