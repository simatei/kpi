# coding: utf-8
import copy
import io
import json
import posixpath
import re
from typing import Union, Optional
import requests
import tempfile
import uuid
from datetime import datetime
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import pytz
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import ugettext_lazy as _
from rest_framework import status
from rest_framework.authtoken.models import Token

from kpi.constants import (
    INSTANCE_FORMAT_TYPE_JSON,
    INSTANCE_FORMAT_TYPE_XML,
    PERM_FROM_KC_ONLY,
)
from kpi.models.object_permission import ObjectPermission
from kpi.utils.log import logging
from kpi.utils.mongo_helper import MongoHelper
from .base_backend import BaseDeploymentBackend
from .kc_access.shadow_models import ReadOnlyKobocatInstance, KobocatXForm
from .kc_access.utils import (
    assign_applicable_kc_permissions,
    instance_count,
    last_submission_time
)
from ..exceptions import (
    BadFormatException,
    KobocatBulkUpdateSubmissionsClientException,
    KobocatDeploymentException,
    KobocatDuplicateSubmissionException,
)


class KobocatDeploymentBackend(BaseDeploymentBackend):
    """
    Used to deploy a project into KC. Stores the project identifiers in the
    "self.asset._deployment_data" JSONField.
    """

    PROTECTED_XML_FIELDS = [
        '__version__',
        'formhub',
        'meta',
    ]

    def bulk_assign_mapped_perms(self):
        """
        Bulk assign all `kc` permissions related to `kpi` permissions.
        Useful to assign permissions retroactively upon deployment.
        Beware: it only adds permissions, it does not remove or sync permissions.
        """
        users_with_perms = self.asset.get_users_with_perms(attach_perms=True)

        # if only the owner has permissions, no need to go further
        if len(users_with_perms) == 1 and \
                list(users_with_perms)[0].id == self.asset.owner_id:
            return

        for user, perms in users_with_perms.items():
            if user.id == self.asset.owner_id:
                continue
            assign_applicable_kc_permissions(self.asset, user, perms)

    @staticmethod
    def make_identifier(username, id_string):
        """
        Uses `settings.KOBOCAT_URL` to construct an identifier from a
        username and id string, without the caller having to specify a server
        or know the full format of KC identifiers
        """
        # No need to use the internal URL here; it will be substituted in when
        # appropriate
        return '{}/{}/forms/{}'.format(
            settings.KOBOCAT_URL,
            username,
            id_string
        )

    @staticmethod
    def external_to_internal_url(url):
        """
        Replace the value of `settings.KOBOCAT_URL` with that of
        `settings.KOBOCAT_INTERNAL_URL` when it appears at the beginning of
        `url`
        """
        return re.sub(
            pattern='^{}'.format(re.escape(settings.KOBOCAT_URL)),
            repl=settings.KOBOCAT_INTERNAL_URL,
            string=url
        )

    @staticmethod
    def internal_to_external_url(url):
        """
        Replace the value of `settings.KOBOCAT_INTERNAL_URL` with that of
        `settings.KOBOCAT_URL` when it appears at the beginning of
        `url`
        """
        return re.sub(
            pattern='^{}'.format(re.escape(settings.KOBOCAT_INTERNAL_URL)),
            repl=settings.KOBOCAT_URL,
            string=url
        )

    @property
    def backend_response(self):
        return self.asset._deployment_data['backend_response']

    def _kobocat_request(self, method, url, **kwargs):
        """
        Make a POST or PATCH request and return parsed JSON. Keyword arguments,
        e.g. `data` and `files`, are passed through to `requests.request()`.
        """

        expected_status_codes = {
            'POST': 201,
            'PATCH': 200,
            'DELETE': 204,
        }
        try:
            expected_status_code = expected_status_codes[method]
        except KeyError:
            raise NotImplementedError(
                'This backend does not implement the {} method'.format(method)
            )

        # Make the request to KC
        try:
            kc_request = requests.Request(method=method, url=url, **kwargs)
            response = self.__kobocat_proxy_request(kc_request, user=self.asset.owner)

        except requests.exceptions.RequestException as e:
            # Failed to access the KC API
            # TODO: clarify that the user cannot correct this
            raise KobocatDeploymentException(detail=str(e))

        # If it's a no-content success, return immediately
        if response.status_code == expected_status_code == 204:
            return {}

        # Parse the response
        try:
            json_response = response.json()
        except ValueError as e:
            # Unparseable KC API output
            # TODO: clarify that the user cannot correct this
            raise KobocatDeploymentException(
                detail=str(e), response=response)

        # Check for failure
        if response.status_code != expected_status_code or (
            'type' in json_response and json_response['type'] == 'alert-error'
        ) or 'formid' not in json_response:
            if 'text' in json_response:
                # KC API refused us for a specified reason, likely invalid
                # input Raise a 400 error that includes the reason
                e = KobocatDeploymentException(detail=json_response['text'])
                e.status_code = status.HTTP_400_BAD_REQUEST
                raise e
            else:
                # Unspecified failure; raise 500
                raise KobocatDeploymentException(
                    detail='Unexpected KoBoCAT error {}: {}'.format(
                        response.status_code, response.content),
                    response=response
                )

        return json_response

    @property
    def timestamp(self):
        try:
            return self.backend_response['date_modified']
        except KeyError:
            return None

    @property
    def xform_id_string(self):
        return self.asset._deployment_data.get('backend_response', {}).get('id_string')

    @property
    def xform_id(self):
        pk = self.asset._deployment_data.get('backend_response', {}).get('formid')
        xform = KobocatXForm.objects.filter(pk=pk).only(
            'user__username', 'id_string').first()
        if not (xform.user.username == self.asset.owner.username and
                xform.id_string == self.xform_id_string):
            raise Exception('Deployment links to an unexpected KoBoCAT XForm')
        return pk

    @property
    def mongo_userform_id(self):
        return '{}_{}'.format(self.asset.owner.username, self.xform_id_string)

    def connect(self, identifier=None, active=False):
        """
        POST initial survey content to kobocat and create a new project.
        store results in self.asset._deployment_data.
        """
        # If no identifier was provided, construct one using
        # `settings.KOBOCAT_URL` and the uid of the asset
        if not identifier:
            # Use the external URL here; the internal URL will be substituted
            # in when appropriate
            if not settings.KOBOCAT_URL or not settings.KOBOCAT_INTERNAL_URL:
                raise ImproperlyConfigured(
                    'Both KOBOCAT_URL and KOBOCAT_INTERNAL_URL must be '
                    'configured before using KobocatDeploymentBackend'
                )
            server = settings.KOBOCAT_URL
            username = self.asset.owner.username
            id_string = self.asset.uid
            identifier = '{server}/{username}/forms/{id_string}'.format(
                server=server,
                username=username,
                id_string=id_string,
            )
        else:
            # Parse the provided identifier, which is expected to follow the
            # format http://kobocat_server/username/forms/id_string
            parsed_identifier = urlparse(identifier)
            server = '{}://{}'.format(
                parsed_identifier.scheme, parsed_identifier.netloc)
            path_head, path_tail = posixpath.split(parsed_identifier.path)
            id_string = path_tail
            path_head, path_tail = posixpath.split(path_head)
            if path_tail != 'forms':
                raise Exception('The identifier is not properly formatted.')
            path_head, path_tail = posixpath.split(path_head)
            if path_tail != self.asset.owner.username:
                raise Exception(
                    'The username in the identifier does not match the owner '
                    'of this asset.'
                )
            if path_head != '/':
                raise Exception('The identifier is not properly formatted.')

        url = self.external_to_internal_url('{}/api/v1/forms'.format(server))
        xls_io = self.asset.to_xls_io(
            versioned=True, append={
                'settings': {
                    'id_string': id_string,
                    'form_title': self.asset.name,
                }
            }
        )

        # Payload contains `kpi_asset_uid` and `has_kpi_hook` for two reasons:
        # - KC `XForm`'s `id_string` can be different than `Asset`'s `uid`, then
        #   we can't rely on it to find its related `Asset`.
        # - Removing, renaming `has_kpi_hook` will force PostgreSQL to rewrite every
        #   records of `logger_xform`. It can be also used to filter queries as it's faster
        #   to query a boolean than string.
        # Don't forget to run Management Command `populate_kc_xform_kpi_asset_uid`
        payload = {
            'downloadable': active,
            'has_kpi_hook': self.asset.has_active_hooks,
            'kpi_asset_uid': self.asset.uid
        }
        files = {'xls_file': ('{}.xls'.format(id_string), xls_io)}
        json_response = self._kobocat_request(
            'POST', url, data=payload, files=files)
        self.store_data({
            'backend': 'kobocat',
            'identifier': self.internal_to_external_url(identifier),
            'active': json_response['downloadable'],
            'backend_response': json_response,
            'version': self.asset.version_id,
        })

    def remove_from_kc_only_flag(self, specific_user: Union[int, 'User'] = None):
        """
        Removes `from_kc_only` flag for ALL USERS unless `specific_user` is
        provided

        Args:
            specific_user (int, User): User object or pk
        """
        # This flag lets us know that permission assignments in KPI exist
        # only because they were copied from KoBoCAT (by `sync_from_kobocat`).
        # As soon as permissions are assigned through KPI, this flag must be
        # removed
        #
        # This method is here instead of `ObjectPermissionMixin` because
        # it's specific to KoBoCat as backend.

        # TODO: Remove this method after kobotoolbox/kobocat#642

        filters = {
            'permission__codename': PERM_FROM_KC_ONLY,
            'asset_id': self.asset.id,
        }
        if specific_user is not None:
            try:
                user_id = specific_user.pk
            except AttributeError:
                user_id = specific_user
            filters['user_id'] = user_id

        ObjectPermission.objects.filter(**filters).delete()

    def redeploy(self, active=None):
        """
        Replace (overwrite) the deployment, keeping the same identifier, and
        optionally changing whether the deployment is active
        """
        if active is None:
            active = self.active
        url = self.external_to_internal_url(self.backend_response['url'])
        id_string = self.backend_response['id_string']
        xls_io = self.asset.to_xls_io(
            versioned=True, append={
                'settings': {
                    'id_string': id_string,
                    'form_title': self.asset.name,
                }
            }
        )
        payload = {
            'downloadable': active,
            'title': self.asset.name,
            'has_kpi_hook': self.asset.has_active_hooks
        }
        files = {'xls_file': ('{}.xls'.format(id_string), xls_io)}
        try:
            json_response = self._kobocat_request(
                'PATCH', url, data=payload, files=files)
            self.store_data({
                'active': json_response['downloadable'],
                'backend_response': json_response,
                'version': self.asset.version_id,
            })
        except KobocatDeploymentException as e:
            if hasattr(e, 'response') and e.response.status_code == 404:
                # Whoops, the KC project we thought we were going to overwrite
                # is gone! Try a standard deployment instead
                return self.connect(self.identifier, active)
            raise

        self.set_asset_uid()

    def set_active(self, active):
        """
        PATCH active boolean of survey.
        store results in self.asset._deployment_data
        """
        # self.store_data is an alias for
        # self.asset._deployment_data.update(...)
        url = self.external_to_internal_url(
            self.backend_response['url'])
        payload = {
            'downloadable': bool(active)
        }
        json_response = self._kobocat_request('PATCH', url, data=payload)
        assert json_response['downloadable'] == bool(active)
        self.store_data({
            'active': json_response['downloadable'],
            'backend_response': json_response,
        })

    def set_asset_uid(self, force=False):
        """
        Link KoBoCAT `XForm` back to its corresponding KPI `Asset` by
        populating the `kpi_asset_uid` field (use KoBoCAT proxy to PATCH XForm).
        Useful when a form is created from the legacy upload form.
        Store results in self.asset._deployment_data

        Returns:
            bool: returns `True` only if `XForm.kpi_asset_uid` field is updated
                  during this call, otherwise `False`.
        """
        is_synchronized = not (
            force or
            self.backend_response.get('kpi_asset_uid', None) is None
        )
        if is_synchronized:
            return False

        url = self.external_to_internal_url(self.backend_response['url'])
        payload = {
            'kpi_asset_uid': self.asset.uid
        }
        json_response = self._kobocat_request('PATCH', url, data=payload)
        is_set = json_response['kpi_asset_uid'] == self.asset.uid
        assert is_set
        self.store_data({
            'backend_response': json_response,
        })
        return True

    def set_has_kpi_hooks(self):
        """
        PATCH `has_kpi_hooks` boolean of survey.
        It lets KoBoCAT know whether it needs to ping KPI
        each time a submission comes in.

        Store results in self.asset._deployment_data
        """
        has_active_hooks = self.asset.has_active_hooks
        url = self.external_to_internal_url(
            self.backend_response['url'])
        payload = {
            'has_kpi_hooks': has_active_hooks,
            'kpi_asset_uid': self.asset.uid
        }

        try:
            json_response = self._kobocat_request('PATCH', url, data=payload)
        except KobocatDeploymentException as e:
            if (
                has_active_hooks is False
                and hasattr(e, 'response')
                and e.response.status_code == status.HTTP_404_NOT_FOUND
            ):
                # It's okay if we're trying to unset the active hooks flag and
                # the KoBoCAT project is already gone. See #2497
                pass
            else:
                raise
        else:
            assert json_response['has_kpi_hooks'] == has_active_hooks
            self.store_data({
                'backend_response': json_response,
            })

    def delete(self):
        """
        WARNING! Deletes all submitted data!
        """
        url = self.external_to_internal_url(self.backend_response['url'])
        try:
            self._kobocat_request('DELETE', url)
        except KobocatDeploymentException as e:
            if (
                hasattr(e, 'response')
                and e.response.status_code == status.HTTP_404_NOT_FOUND
            ):
                # The KC project is already gone!
                pass
            else:
                raise
        super().delete()

    def delete_submission(self, pk, user):
        """
        Deletes submission through KoBoCAT proxy
        :param pk: int
        :param user: User
        :return: dict
        """
        kc_url = self.get_submission_detail_url(pk)
        kc_request = requests.Request(method="DELETE", url=kc_url)
        kc_response = self.__kobocat_proxy_request(kc_request, user)

        return self.__prepare_as_drf_response_signature(kc_response)

    def delete_submissions(self, data, user):
        """
        Deletes submissions through KoBoCAT proxy
        :param user: User
        :return: dict
        """

        kc_url = self.submission_list_url
        kc_request = requests.Request(method='DELETE', url=kc_url, data=data)
        kc_response = self.__kobocat_proxy_request(kc_request, user)

        return self.__prepare_as_drf_response_signature(kc_response)

    def get_enketo_survey_links(self):
        data = {
            'server_url': '{}/{}'.format(
                settings.KOBOCAT_URL.rstrip('/'),
                self.asset.owner.username
            ),
            'form_id': self.backend_response['id_string']
        }
        try:
            response = requests.post(
                '{}{}'.format(
                    settings.ENKETO_SERVER, settings.ENKETO_SURVEY_ENDPOINT),
                # bare tuple implies basic auth
                auth=(settings.ENKETO_API_TOKEN, ''),
                data=data
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            # Don't 500 the entire asset view if Enketo is unreachable
            logging.error(
                'Failed to retrieve links from Enketo', exc_info=True)
            return {}
        try:
            links = response.json()
        except ValueError:
            logging.error('Received invalid JSON from Enketo', exc_info=True)
            return {}
        for discard in ('enketo_id', 'code', 'preview_iframe_url'):
            try:
                del links[discard]
            except KeyError:
                pass
        return links

    def get_data_download_links(self):
        exports_base_url = '/'.join((
            settings.KOBOCAT_URL.rstrip('/'),
            self.asset.owner.username,
            'exports',
            self.backend_response['id_string']
        ))
        reports_base_url = '/'.join((
            settings.KOBOCAT_URL.rstrip('/'),
            self.asset.owner.username,
            'reports',
            self.backend_response['id_string']
        ))
        forms_base_url = '/'.join((
            settings.KOBOCAT_URL.rstrip('/'),
            self.asset.owner.username,
            'forms',
            self.backend_response['id_string']
        ))
        links = {
            # To be displayed in iframes
            'xls_legacy': '/'.join((exports_base_url, 'xls/')),
            'csv_legacy': '/'.join((exports_base_url, 'csv/')),
            'zip_legacy': '/'.join((exports_base_url, 'zip/')),
            'kml_legacy': '/'.join((exports_base_url, 'kml/')),
            # For GET requests that return files directly
            'xls': '/'.join((reports_base_url, 'export.xlsx')),
            'csv': '/'.join((reports_base_url, 'export.csv')),
        }
        return links

    def _submission_count(self):
        _deployment_data = self.asset._deployment_data
        id_string = _deployment_data['backend_response']['id_string']
        # avoid migrations from being created for kc_access mocked models
        # there should be a better way to do this, right?
        return instance_count(xform_id_string=id_string,
                              user_id=self.asset.owner.pk,
                              )

    @property
    def submission_list_url(self):
        url = '{kc_base}/api/v1/data/{formid}'.format(
            kc_base=settings.KOBOCAT_INTERNAL_URL,
            formid=self.backend_response['formid']
        )
        return url

    @property
    def submission_url(self) -> str:
        url = '{kc_base}/submission'.format(
            kc_base=settings.KOBOCAT_URL,
        )
        return url

    def get_submission_detail_url(self, submission_pk):
        url = '{list_url}/{pk}'.format(
            list_url=self.submission_list_url,
            pk=submission_pk
        )
        return url

    def get_submission_edit_url(self, submission_pk, user, params=None):
        """
        Gets edit URL of the submission from `kc` through proxy

        :param submission_pk: int
        :param user: User
        :param params: dict
        :return: dict
        """
        url = '{detail_url}/enketo'.format(
            detail_url=self.get_submission_detail_url(submission_pk))
        kc_request = requests.Request(method='GET', url=url, params=params)
        kc_response = self.__kobocat_proxy_request(kc_request, user)

        return self.__prepare_as_drf_response_signature(kc_response)

    def _last_submission_time(self):
        _deployment_data = self.asset._deployment_data
        id_string = _deployment_data['backend_response']['id_string']
        return last_submission_time(
            xform_id_string=id_string, user_id=self.asset.owner.pk)

    def get_submission_validation_status_url(self, submission_pk):
        url = '{detail_url}/validation_status'.format(
            detail_url=self.get_submission_detail_url(submission_pk)
        )
        return url

    def get_submissions(self, requesting_user_id,
                        format_type=INSTANCE_FORMAT_TYPE_JSON,
                        instance_ids=[], **kwargs):
        """
        Retrieves submissions through Postgres or Mongo depending on `format_type`.
        It can be filtered on instances ids.

        Args:
            requesting_user_id (int)
            format_type (str): INSTANCE_FORMAT_TYPE_JSON|INSTANCE_FORMAT_TYPE_XML
            instance_ids (list): Instance ids to retrieve
            kwargs (dict): Filters to pass to MongoDB. See
                https://docs.mongodb.com/manual/reference/operator/query/

        Returns:
            (dict|str|`None`): Depending of `format_type`, it can return:
                - Mongo JSON representation as a dict
                - Instances' XML as string
                - `None` if no results
        """

        kwargs['instance_ids'] = instance_ids
        params = self.validate_submission_list_params(requesting_user_id,
                                                      format_type=format_type,
                                                      **kwargs)

        if format_type == INSTANCE_FORMAT_TYPE_JSON:
            submissions = self.__get_submissions_in_json(**params)
        elif format_type == INSTANCE_FORMAT_TYPE_XML:
            submissions = self.__get_submissions_in_xml(**params)
        else:
            raise BadFormatException(
                "The format {} is not supported".format(format_type)
            )
        return submissions

    @staticmethod
    def generate_new_instance_id() -> (str, str):
        """
        Returns:
            - Generated uuid
            - Formatted uuid for OpenRosa xml
        """
        _uuid = str(uuid.uuid4())
        return _uuid, f'uuid:{_uuid}'

    @staticmethod
    def format_openrosa_datetime(dt: Optional[datetime] = None) -> str:
        """
        Format a given datetime object or generate a new timestamp matching the
        OpenRosa datetime formatting
        """
        if dt is None:
            dt = datetime.now(tz=pytz.UTC)

        # Awkward check, but it's prescribed by
        # https://docs.python.org/3/library/datetime.html#determining-if-an-object-is-aware-or-naive
        if dt.tzinfo is None or dt.tzinfo.utcoffset(None) is None:
            raise ValueError('An offset-aware datetime is required')
        return dt.isoformat('T', 'milliseconds')

    def duplicate_submission(
        self, requesting_user: 'auth.User', instance_id: int
    ) -> dict:
        """
        Duplicates a single submission proxied through kobocat. The submission
        with the given `instance_id` is duplicated and the `start`, `end` and
        `instanceID` parameters of the submission are reset before being posted
        to kobocat.

        Args:
            requesting_user ('auth.User')
            instance_id (int)

        Returns:
            dict: message response from kobocat and uuid of created submission
            if successful
        """
        params = self.validate_submission_list_params(
            requesting_user.id,
            format_type=INSTANCE_FORMAT_TYPE_XML,
            instance_ids=[instance_id],
        )
        submissions = self.__get_submissions_in_xml(**params)

        # parse XML string to ET object
        xml_parsed = ET.fromstring(next(submissions))

        # attempt to update XML fields for duplicate submission. Note that
        # `start` and `end` are not guaranteed to be included in the XML object
        _uuid, uuid_formatted = self.generate_new_instance_id()
        date_formatted = self.format_openrosa_datetime()
        for date_field in ('start', 'end'):
            element = xml_parsed.find(date_field)
            # Even if the element is found, `bool(element)` is `False`. How
            # very un-Pythonic!
            if element is not None:
                element.text = date_formatted
        # Rely on `meta/instanceID` being present. If it's absent, something is
        # fishy enough to warrant raising an exception instead of continuing
        # silently
        xml_parsed.find('meta/instanceID').text = uuid_formatted

        file_tuple = (_uuid, io.BytesIO(ET.tostring(xml_parsed)))
        files = {'xml_submission_file': file_tuple}
        kc_request = requests.Request(
            method='POST', url=self.submission_url, files=files
        )
        kc_response = self.__kobocat_proxy_request(
            kc_request, user=requesting_user
        )

        if kc_response.status_code == status.HTTP_201_CREATED:
            return next(
                self.get_submissions(requesting_user.id, query={'_uuid': _uuid})
            )
        else:
            raise KobocatDuplicateSubmissionException

    def get_validation_status(self, submission_pk, params, user):
        url = self.get_submission_validation_status_url(submission_pk)
        kc_request = requests.Request(method='GET', url=url, data=params)
        kc_response = self.__kobocat_proxy_request(kc_request, user)
        return self.__prepare_as_drf_response_signature(kc_response)

    def set_validation_status(self, submission_pk, data, user, method):
        """
        Updates validation status from `kc` through proxy
        If method is `DELETE`, it resets the status to `None`

        Args:
            submission_pk (int)
            data (dict): data to update when `PATCH` is used.
            user (User)
            method (string): 'PATCH'|'DELETE'

        Returns:
            dict (a formatted dict to be passed to a Response object)
        """
        kc_request_params = {
            'method': method,
            'url': self.get_submission_validation_status_url(submission_pk)
        }
        if method == 'PATCH':
            kc_request_params.update({
                'json': data
            })
        kc_request = requests.Request(**kc_request_params)
        kc_response = self.__kobocat_proxy_request(kc_request, user)
        return self.__prepare_as_drf_response_signature(kc_response)

    def set_validation_statuses(self, data, user, method):
        """
        Bulk update for validation status from `kc` through proxy
        If method is `DELETE`, it resets statuses to `None`

        Args:
            data (dict): data to update when `PATCH` is used.
            user (User)
            method (string): 'PATCH'|'DELETE'

        Returns:
            dict (a formatted dict to be passed to a Response object)
        """
        url = self.submission_list_url
        data = data.copy()  # Need to get a copy to update the dict

        # `PATCH` KC even if kpi receives `DELETE`
        kc_request = requests.Request(method='PATCH', url=url, json=data)
        kc_response = self.__kobocat_proxy_request(kc_request, user)
        return self.__prepare_as_drf_response_signature(kc_response)

    def bulk_update_submissions(
        self, request_data: dict, requesting_user: 'auth.User'
    ) -> dict:
        """
        Allows for bulk updating of submissions proxied through kobocat. A
        `deprecatedID` for each submission is given the previous value of
        `instanceID` and `instanceID` receives an updated uuid. For each key
        and value within `request_data`, either a new element is created on the
        submission's XML tree, or the existing value is replaced by the updated
        value.

        Args:
            request_data (dict): must contain a list of `submission_ids` and at
                least one other key:value field for updating the submissions
            requesting_user ('auth.User')

        Returns:
            dict: formatted dict to be passed to a Response object
        """
        payload = self.__prepare_bulk_update_payload(request_data)
        kwargs = {'instance_ids': payload.pop('submission_ids')}
        params = self.validate_submission_list_params(
            requesting_user.id, format_type=INSTANCE_FORMAT_TYPE_XML, **kwargs
        )
        submissions = list(self.__get_submissions_in_xml(**params))
        validated_submissions = self.__validate_bulk_update_submissions(
            submissions
        )

        kc_responses = []
        for submission in validated_submissions:
            xml_parsed = ET.fromstring(submission)

            _uuid, uuid_formatted = self.generate_new_instance_id()

            # Updating xml fields for submission. In order to update an existing
            # submission, the current `instanceID` must be moved to the value
            # for `deprecatedID`.
            instance_id = xml_parsed.find('meta/instanceID')
            # If the submission has been edited before, it will already contain
            # a deprecatedID element - otherwise create a new element
            deprecated_id = xml_parsed.find('meta/deprecatedID')
            deprecated_id_or_new = (
                deprecated_id
                if deprecated_id is not None
                else ET.SubElement(xml_parsed.find('meta'), 'deprecatedID')
            )
            deprecated_id_or_new.text = instance_id.text
            instance_id.text = uuid_formatted

            # If the form has been updated with new fields and earlier
            # submissions have been selected as part of the bulk update,
            # a new element has to be created before a value can be set.
            # However, with this new power, arbitrary fields can be added
            # to the XML tree through the API.
            for k, v in payload['data'].items():
                # A potentially clunky way of taking groups and nested groups
                # into account when the elements don't exist on the XML tree
                # (which could be the case if the form has been updated). They
                # are iteratively attached to the tree since we can only
                # append one element deep per iteration
                if '/' in k:
                    accumulated_elements = []
                    for i, element in enumerate(k.split('/')):
                        if i == 0:
                            ET.SubElement(xml_parsed, element)
                            accumulated_elements.append(element)
                        else:
                            updated_xml_path = '/'.join(accumulated_elements)
                            ET.SubElement(
                                xml_parsed.find(updated_xml_path), element
                            )
                            accumulated_elements.append(element)

                element_to_update = xml_parsed.find(k)
                element_to_update_or_new = (
                    element_to_update
                    if element_to_update is not None
                    else ET.SubElement(xml_parsed, k)
                )
                element_to_update_or_new.text = v

            # TODO: Might be worth refactoring this as it is also used when
            # duplicating a submission
            file_tuple = (_uuid, io.BytesIO(ET.tostring(xml_parsed)))
            files = {'xml_submission_file': file_tuple}
            # `POST` is required by OpenRosa spec https://docs.getodk.org/openrosa-form-submission
            kc_request = requests.Request(
                method='POST', url=self.submission_url, files=files
            )
            kc_response = self.__kobocat_proxy_request(
                kc_request, user=requesting_user
            )

            kc_responses.append(
                {
                    'uuid': _uuid,
                    'response': kc_response,
                }
            )

        return self.__prepare_bulk_update_response(kc_responses)

    def calculated_submission_count(self, requesting_user_id, **kwargs):
        params = self.validate_submission_list_params(requesting_user_id,
                                                      validate_count=True,
                                                      **kwargs)
        return MongoHelper.get_count(self.mongo_userform_id, **params)

    def __get_submissions_in_json(self, **params):
        """
        Retrieves instances directly from Mongo.

        :param params: dict. Filter params
        :return: generator<JSON>
        """

        instances, total_count = MongoHelper.get_instances(
            self.mongo_userform_id, **params)

        # Python-only attribute used by `kpi.views.v2.data.DataViewSet.list()`
        self.current_submissions_count = total_count

        return (
            MongoHelper.to_readable_dict(instance)
            for instance in instances
        )

    def __get_submissions_in_xml(self, **params):
        """
        Retrieves instances directly from Postgres.

        :param params: dict. Filter params
        :return: list<XML>
        """

        mongo_filters = ['query', 'permission_filters']
        use_mongo = any(mongo_filter in mongo_filters for mongo_filter in params
                        if params.get(mongo_filter) is not None)

        if use_mongo:
            # We use Mongo to retrieve matching instances.
            # Get only their ids and pass them to PostgreSQL.
            params['fields'] = [self.INSTANCE_ID_FIELDNAME]
            # Force `sort` by `_id` for Mongo
            # See FIXME about sort in `BaseDeploymentBackend.validate_submission_list_params()`
            params['sort'] = {self.INSTANCE_ID_FIELDNAME: 1}
            instances, count = MongoHelper.get_instances(self.mongo_userform_id,
                                                         **params)
            instance_ids = [instance.get(self.INSTANCE_ID_FIELDNAME) for instance in
                            instances]
            self.current_submissions_count = count

        queryset = ReadOnlyKobocatInstance.objects.filter(
            xform_id=self.xform_id,
            deleted_at=None
        )

        if len(instance_ids) > 0 or use_mongo:
            queryset = queryset.filter(id__in=instance_ids)

        # Python-only attribute used by `kpi.views.v2.data.DataViewSet.list()`
        if not use_mongo:
            self.current_submissions_count = queryset.count()

        # Force Sort by id
        # See FIXME about sort in `BaseDeploymentBackend.validate_submission_list_params()`
        queryset = queryset.order_by('id')

        # When using Mongo, data is already paginated, no need to do it with PostgreSQL too.
        if not use_mongo:
            offset = params.get('start')
            limit = offset + params.get('limit')
            queryset = queryset[offset:limit]

        return (lazy_instance.xml for lazy_instance in queryset)

    @staticmethod
    def __kobocat_proxy_request(kc_request, user=None):
        """
        Send `kc_request`, which must specify `method` and `url` at a minimum.
        If the incoming request to be proxied is authenticated,
        logged-in user's API token will be added to `kc_request.headers`

        :param kc_request: requests.models.Request
        :param user: User
        :return: requests.models.Response
        """
        if not user.is_anonymous and user.pk != settings.ANONYMOUS_USER_ID:
            token, created = Token.objects.get_or_create(user=user)
            kc_request.headers['Authorization'] = 'Token %s' % token.key
        session = requests.Session()
        return session.send(kc_request.prepare())

    @staticmethod
    def __prepare_as_drf_response_signature(requests_response):
        """
        Prepares a dict from `Requests` response.
        Useful to get response from `kc` and use it as a dict or pass it to
        DRF Response
        """

        prepared_drf_response = {}

        # `requests_response` may not have `headers` attribute
        content_type = requests_response.headers.get('Content-Type')
        content_language = requests_response.headers.get('Content-Language')
        if content_type:
            prepared_drf_response['content_type'] = content_type
        if content_language:
            prepared_drf_response['headers'] = {
                'Content-Language': content_language
            }

        prepared_drf_response['status'] = requests_response.status_code

        try:
            prepared_drf_response['data'] = json.loads(requests_response.content)
        except ValueError as e:
            if not requests_response.status_code == status.HTTP_204_NO_CONTENT:
                prepared_drf_response['data'] = {
                    'detail': _(
                        'KoBoCAT returned an unexpected response: {}'.format(str(e))
                    )
                }

        return prepared_drf_response

    @staticmethod
    def __validate_bulk_update_payload(payload: dict) -> dict:
        """
        Validating the request payload for bulk updating of submissions
        """
        if not 'submission_ids' in payload:
            raise KobocatBulkUpdateSubmissionsClientException(
                detail=_('`submission_ids` must be included in the payload')
            )

        if not isinstance(payload['submission_ids'], list):
            raise KobocatBulkUpdateSubmissionsClientException(
                detail=_('`submission_ids` must be an array')
            )

        if len(payload['submission_ids']) == 0:
            raise KobocatBulkUpdateSubmissionsClientException(
                detail=_('`submission_ids` must contain at least one value')
            )

        if not 'data' in payload:
            raise KobocatBulkUpdateSubmissionsClientException(
                detail=_('`data` must be included in the payload')
            )

        if len(payload['data']) == 0:
            raise KobocatBulkUpdateSubmissionsClientException(
                detail=_('Payload must contain data to update the submissions')
            )

        return payload

    @classmethod
    def __prepare_bulk_update_payload(cls, request_data: dict) -> dict:
        """
        Preparing the request payload for bulk updating of submissions
        """
        payload = json.loads(request_data['payload'][0])
        validated_payload = cls.__validate_bulk_update_payload(payload)

        # Ensuring submission ids are integer values and unique
        try:
            validated_payload['submission_ids'] = list(
                set(map(int, validated_payload['submission_ids']))
            )
        except ValueError as e:
            raise KobocatBulkUpdateSubmissionsClientException(
                detail=_('`submission_ids` must only contain integer values')
            )

        # Sanitizing the payload of potentially destructive keys
        santized_payload = copy.deepcopy(validated_payload)
        for key in validated_payload['data']:
            if key in cls.PROTECTED_XML_FIELDS or (
                '/' in key and key.split('/')[0] in cls.PROTECTED_XML_FIELDS
            ):
                santized_payload['data'].pop(key)

        return santized_payload

    @staticmethod
    def __validate_bulk_update_submissions(submissions: list) -> list:
        if len(submissions) == 0:
            raise KobocatBulkUpdateSubmissionsClientException(
                detail=_('No submissions match the given `submission_ids`')
            )
        return submissions

    @staticmethod
    def __prepare_bulk_update_response(kc_responses: list) -> dict:
        '''
        Formatting the response to allow for partial successes to be seen
        more explicitly.

        Args:
            kc_responses (list): A list containing dictionaries with keys of
            `_uuid` from the newly generated uuid and `response`, the response
            object received from Kobocat

        Returns:
            dict: formatted dict to be passed to a Response object and sent to
            the client
        '''

        OPEN_ROSA_XML_MESSAGE = '{http://openrosa.org/http/response}message'

        # Unfortunately, the response message from OpenRosa is in XML format,
        # so it needs to be parsed before extracting the text
        results = []
        for response in kc_responses:
            try:
                message = _(
                    ET.fromstring(response['response'].content)
                    .find(OPEN_ROSA_XML_MESSAGE)
                    .text
                )
            except ET.ParseError:
                message = _('Something went wrong')

            results.append(
                {
                    'uuid': response['uuid'],
                    'status_code': response['response'].status_code,
                    'message': message,
                }
            )

        total_update_attempts = len(results)
        total_successes = [result['status_code'] for result in results].count(
            status.HTTP_201_CREATED
        )

        return {
            'status': status.HTTP_200_OK
            if total_successes > 0
            else status.HTTP_400_BAD_REQUEST,
            'data': {
                'count': total_update_attempts,
                'successes': total_successes,
                'failures': total_update_attempts - total_successes,
                'results': results,
            },
        }

