'''
A python representation of a valid jsonschema with all the varieties of questions
found in a form in kpi's Asset.content structure.

Note: This is a draft, intended to help mark the XLSForm validity
of Asset.content after every change
'''

ALL_KOBO_TYPES = ['today',
                  'audit',
                  'barcode',
                  'audio',
                  'begin_repeat',
                  'end_repeat',
                  'image',
                  'acknowledge',
                  'username',
                  'simserial',
                  'subscriberid',
                  'deviceid',
                  'phonenumber',
                  'start',
                  'end',
                  'begin_group',
                  'end_group',
                  'begin_kobomatrix',
                  'begin_score',
                  'begin_rank',
                  'end_rank',
                  'rank__level',
                  'score__row',
                  'end_score',
                  'calculate',
                  'date',
                  'datetime',
                  'decimal',
                  'end',
                  'end_kobomatrix',
                  'file',
                  'filterType',
                  'geopoint',
                  'geoshape',
                  'geotrace',
                  'image',
                  'int',
                  'integer',
                  'note',
                  'osm',
                  'osm_buildingtags',
                  'range',
                  'select_multiple',
                  'select_multiple_from_file',
                  'select_one',
                  'select_one_external',
                  'select_one_from_file',
                  'start',
                  'string',
                  'text',
                  'time',
                  'xml-external']

DRAFT_FORM_SCHEMA = {'$digest': 'fb2584ef',
 '$id': 'https://xlsform.org/schema.json',
 '$schema': 'http://json-schema.org/draft-07/schema#',
 'additionalProperties': False,
 'definitions': {'group': {'properties': {'children': {'items': {'$ref': '#/definitions/groupOrRow'},
                                                       'type': 'array'},
                                          'type': {'$ref': '#/definitions/groupTypes'}},
                           'type': 'object'},
                 'groupOrRow': {'anyOf': [{'$ref': '#/definitions/row'},
                                          {'$ref': '#/definitions/group'}],
                                'type': 'object'},
                 'groupTypes': {'enum': ['group', 'repeat'], 'type': 'string'},
                 'row': {'properties': {'label': {'$ref': '#/definitions/translatable'},
                                        'type': {'$ref': '#/definitions/rowTypes'}},
                         'type': 'object'},
                 'rowTypes': {'enum': ALL_KOBO_TYPES,
                              'type': 'string'},
                 'strings': {'type': 'string'},
                 'translatable': {'anyOf': [{'$ref': '#/definitions/translated'},
                                            {'$ref': '#/definitions/strings'}]},
                 'translated': {'properties': {'en': {'$ref': '#/definitions/strings'},
                                               'fr': {'$ref': '#/definitions/strings'}},
                                'type': 'object'}},
 'properties': {'choices': {'type': 'object'},
                'schema': {'type': 'string'},
                'settings': {'properties': {'identifier': {'type': 'string'}},
                             'type': 'object'},
                'survey': {'items': {'$ref': '#/definitions/groupOrRow'},
                           'type': 'array'},
                'translations': {'items': {'additionalProperties': False,
                                           'properties': {'$txid': {'type': 'string'},
                                                          'code': {'type': 'string'},
                                                          'name': {'type': 'string'}},
                                           'type': 'object'},
                                 'type': 'array'}},
 'type': 'object'}
