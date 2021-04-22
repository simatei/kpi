# coding: utf-8
from django.utils.translation import gettext as _
from rest_framework import serializers
from rest_framework.fields import empty

from kpi.constants import (
    PERM_CHANGE_SUBMISSIONS,
    PERM_VALIDATE_SUBMISSIONS,
)
from kpi.fields import WritableJSONField
from kpi.utils.iterators import to_int


class DataBulkActionsValidator(serializers.Serializer):

    payload = WritableJSONField()

    ALLOWED_PERM_TO_MODIFY_ALL = [
        PERM_VALIDATE_SUBMISSIONS,
    ]

    def __init__(self, instance=None, data=empty, **kwargs):
        self.__perm = kwargs.pop('perm', None)
        super().__init__(instance=instance, data=data, **kwargs)

    def validate_payload(self, payload: dict) -> dict:
        try:
            payload['submission_ids']
        except KeyError:
            if self.__perm not in self.ALLOWED_PERM_TO_MODIFY_ALL:
                raise serializers.ValidationError(
                    _('`submission_ids` is required')
                )

            # If `query` is not provided, it means that all submissions should
            # be altered. In that case, `confirm=True` should be passed among
            # the parameters to validate the action
            try:
                payload['query']
            except KeyError:
                if not payload.get('confirm', False):
                    raise serializers.ValidationError(
                        _('Confirmation is required')
                    )

        else:
            self.__validate_submission_ids(payload)
            if self.__perm == PERM_CHANGE_SUBMISSIONS:
                self.__validate_updated_data(payload)

        return payload

    def to_representation(self, instance):
        return {
            'submission_ids': instance['payload'].get('submission_ids', []),
            'query': instance['payload'].get('query', {}),
            'data': instance['payload'].get('data'),
        }

    def __validate_submission_ids(self, payload: dict):
        try:
            # Ensuring submission ids are integer values and unique
            submission_ids = to_int(payload['submission_ids'], unique=True)
        except ValueError:
            raise serializers.ValidationError(
                _('`submission_ids` must only contain integer values')
            )

        if len(submission_ids) == 0:
            raise serializers.ValidationError(
                _('`submission_ids` must contain at least one value')
            )

        payload['submission_ids'] = submission_ids

    def __validate_updated_data(self, payload: dict):
        if not payload.get('data'):
            raise serializers.ValidationError(
                _('`data` is required')
            )
