"""Allowlisted view of stored config only; no env or filesystem reads."""

def _scalars(source, fields):
    result = {}
    for key, kind in fields.items():
        value = source.get(key)
        if value is not None and type(value) is not kind:
            raise ValueError('Stored configuration field has invalid type')
        result[key] = value
    return result


def public_stored_document(document, current):
    if document not in {'mail', 'llm'}:
        raise ValueError('Unsupported configuration document')
    if current is None:
        return {'configured': False, 'source': 'none'}
    if not isinstance(current, dict):
        raise ValueError('Stored configuration document is invalid')
    result = {'configured': True, 'source': 'database'}
    if document == 'llm':
        result.update(_scalars(current, {'base_url':str,'model':str,'timeout':int,'enabled':bool}))
        result.update(api_key_set=bool(current.get('api_key')),api_key_mask='********' if current.get('api_key') else '')
    else:
        slots = current.get('slots',{})
        if not isinstance(slots,dict):
            raise ValueError('Stored mail slots are invalid')
        result['slots'] = {}
        for name in ('transactional','notification'):
            slot = slots.get(name,{})
            if not isinstance(slot,dict):
                raise ValueError('Stored mail slot is invalid')
            public = _scalars(slot, {'host':str,'port':int,'security':str,'username':str,'from_address':str,'from_name':str,'enabled':bool})
            public.update(password_set=bool(slot.get('password')),password_mask='********' if slot.get('password') else '')
            result['slots'][name] = public
    # No spread/merge of unknown fields, legacy payloads or secret values.
    return result
