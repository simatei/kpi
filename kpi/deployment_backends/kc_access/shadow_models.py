# coding: utf-8
from hashlib import md5

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.postgres.fields import JSONField as JSONBField
from django.db import (
    ProgrammingError,
    connections,
    models,
    router,
)
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from django_digest.models import PartialDigest

from kpi.constants import SHADOW_MODEL_APP_LABEL
from kpi.exceptions import BadContentTypeException
from kpi.utils.strings import hashable_str


def update_autofield_sequence(model):
    """
    Fixes the PostgreSQL sequence for the first (and only?) `AutoField` on
    `model`, à la `manage.py sqlsequencereset`
    """
    sql_template = (
        "SELECT setval(pg_get_serial_sequence('{table}','{column}'), "
        "coalesce(max({column}), 1), max({column}) IS NOT null) FROM {table};"
    )
    autofield = None
    for f in model._meta.get_fields():
        if isinstance(f, models.AutoField):
            autofield = f
            break
    if not autofield:
        return
    query = sql_template.format(
        table=model._meta.db_table, column=autofield.column
    )
    connection = connections[router.db_for_write(model)]
    with connection.cursor() as cursor:
        cursor.execute(query)


class ShadowModel(models.Model):
    """
    Allows identification of writeable and read-only shadow models
    """
    class Meta:
        managed = False
        abstract = True
        # TODO find out why it raises a warning when user logs in.
        # ```
        #   RuntimeWarning: Model '...' was already registered.
        #   Reloading models is not advised as it can lead to inconsistencies,
        #   most notably with related models
        # ```
        # Maybe because `SHADOW_MODEL_APP_LABEL` is not declared in `INSTALLED_APP`
        # It's just used for `DefaultDatabaseRouter` conditions.
        app_label = SHADOW_MODEL_APP_LABEL

    @staticmethod
    def get_content_type_for_model(model):
        model_name_mapping = {
            'kobocatxform': ('logger', 'xform'),
            'readonlykobocatinstance': ('logger', 'instance'),
            'kobocatuserprofile': ('main', 'userprofile'),
            'kobocatuserobjectpermission': ('guardian', 'userobjectpermission'),
        }
        try:
            app_label, model_name = model_name_mapping[model._meta.model_name]
        except KeyError:
            raise NotImplementedError
        return KobocatContentType.objects.get(
            app_label=app_label, model=model_name)


class ReadOnlyShadowModel(ShadowModel):

    read_only = True

    class Meta(ShadowModel.Meta):
        abstract = True


class KobocatXForm(ShadowModel):

    class Meta(ShadowModel.Meta):
        db_table = 'logger_xform'
        verbose_name = 'xform'
        verbose_name_plural = 'xforms'

    XFORM_TITLE_LENGTH = 255
    xls = models.FileField(null=True)
    xml = models.TextField()
    user = models.ForeignKey('KobocatUser', related_name='xforms', null=True,
                             on_delete=models.CASCADE)
    shared = models.BooleanField(default=False)
    shared_data = models.BooleanField(default=False)
    downloadable = models.BooleanField(default=True)
    id_string = models.SlugField()
    title = models.CharField(max_length=XFORM_TITLE_LENGTH)
    date_created = models.DateTimeField()
    date_modified = models.DateTimeField()
    uuid = models.CharField(max_length=32, default='')
    last_submission_time = models.DateTimeField(blank=True, null=True)
    num_of_submissions = models.IntegerField(default=0)

    @property
    def hash(self):
        return '%s' % md5(hashable_str(self.xml)).hexdigest()

    @property
    def prefixed_hash(self):
        """
        Matches what's returned by the KC API
        """

        return "md5:%s" % self.hash


class ReadOnlyKobocatInstance(ReadOnlyShadowModel):

    class Meta(ReadOnlyShadowModel.Meta):
        db_table = 'logger_instance'
        verbose_name = 'instance'
        verbose_name_plural = 'instances'

    xml = models.TextField()
    user = models.ForeignKey(User, null=True, on_delete=models.CASCADE)
    xform = models.ForeignKey(KobocatXForm, related_name='instances',
                              on_delete=models.CASCADE)
    date_created = models.DateTimeField()
    date_modified = models.DateTimeField()
    deleted_at = models.DateTimeField(null=True, default=None)
    status = models.CharField(max_length=20,
                              default='submitted_via_web')
    uuid = models.CharField(max_length=249, default='')


class KobocatContentType(ShadowModel):
    """
    Minimal representation of Django 1.8's
    contrib.contenttypes.models.ContentType
    """
    app_label = models.CharField(max_length=100)
    model = models.CharField(_('python model class name'), max_length=100)

    class Meta(ShadowModel.Meta):
        db_table = 'django_content_type'
        unique_together = (('app_label', 'model'),)

    def __str__(self):
        # Not as nice as the original, which returns a human-readable name
        # complete with whitespace. That requires access to the Python model
        # class, though
        return self.model


class KobocatPermission(ShadowModel):
    """
    Minimal representation of Django 1.8's contrib.auth.models.Permission
    """
    name = models.CharField(_('name'), max_length=255)
    content_type = models.ForeignKey(KobocatContentType, on_delete=models.CASCADE)
    codename = models.CharField(_('codename'), max_length=100)

    class Meta(ShadowModel.Meta):
        db_table = 'auth_permission'
        unique_together = (('content_type', 'codename'),)
        ordering = ('content_type__app_label', 'content_type__model',
                    'codename')

    def __str__(self):
        return "%s | %s | %s" % (
            str(self.content_type.app_label),
            str(self.content_type),
            str(self.name))


class KobocatUser(ShadowModel):

    username = models.CharField(_("username"), max_length=30)
    password = models.CharField(_("password"), max_length=128)
    last_login = models.DateTimeField(_("last login"), blank=True, null=True)
    is_superuser = models.BooleanField(_('superuser status'), default=False)
    first_name = models.CharField(_('first name'), max_length=30, blank=True)
    last_name = models.CharField(_('last name'), max_length=150, blank=True)
    email = models.EmailField(_('email address'), blank=True)
    is_staff = models.BooleanField(_('staff status'), default=False)
    is_active = models.BooleanField(_('active'), default=True)
    date_joined = models.DateTimeField(_('date joined'), default=timezone.now)

    class Meta(ShadowModel.Meta):
        db_table = "auth_user"

    @classmethod
    def sync(cls, auth_user):
        # NB: `KobocatUserObjectPermission` (and probably other things) depend
        # upon PKs being synchronized between KPI and KoBoCAT
        try:
            kc_auth_user = cls.objects.get(pk=auth_user.pk)
            assert kc_auth_user.username == auth_user.username
        except KobocatUser.DoesNotExist:
            kc_auth_user = cls(pk=auth_user.pk, username=auth_user.username)

        kc_auth_user.password = auth_user.password
        kc_auth_user.last_login = auth_user.last_login
        kc_auth_user.is_superuser = auth_user.is_superuser
        kc_auth_user.first_name = auth_user.first_name
        kc_auth_user.last_name = auth_user.last_name
        kc_auth_user.email = auth_user.email
        kc_auth_user.is_staff = auth_user.is_staff
        kc_auth_user.is_active = auth_user.is_active
        kc_auth_user.date_joined = auth_user.date_joined

        kc_auth_user.save()

        # We've manually set a primary key, so `last_value` in the sequence
        # `auth_user_id_seq` now lags behind `max(id)`. Fix it now!
        update_autofield_sequence(cls)

        # Update django-digest `PartialDigest`s in KoBoCAT.  This is only
        # necessary if the user's password has changed, but we do it always
        KobocatDigestPartial.sync(kc_auth_user)


class KobocatUserObjectPermission(ShadowModel):
    """
    For the _sole purpose_ of letting us manipulate KoBoCAT
    permissions, this comprises the following django-guardian classes
    all condensed into one:

      * UserObjectPermission
      * UserObjectPermissionBase
      * BaseGenericObjectPermission
      * BaseObjectPermission

    CAVEAT LECTOR: The django-guardian custom manager,
    UserObjectPermissionManager, is NOT included!
    """
    permission = models.ForeignKey(KobocatPermission, on_delete=models.CASCADE)
    content_type = models.ForeignKey(KobocatContentType, on_delete=models.CASCADE)
    object_pk = models.CharField(_('object ID'), max_length=255)
    content_object = GenericForeignKey(fk_field='object_pk')
    # It's okay not to use `KobocatUser` as long as PKs are synchronized
    user = models.ForeignKey(
        getattr(settings, 'AUTH_USER_MODEL', 'auth.User'),
        on_delete=models.CASCADE)

    class Meta(ShadowModel.Meta):
        db_table = 'guardian_userobjectpermission'
        unique_together = ['user', 'permission', 'object_pk']

    def __str__(self):
        # `unicode(self.content_object)` fails when the object's model
        # isn't known to this Django project. Let's use something more
        # benign instead.
        content_object_str = '{app_label}_{model} ({pk})'.format(
            app_label=self.content_type.app_label,
            model=self.content_type.model,
            pk=self.object_pk)
        return '%s | %s | %s' % (
            # unicode(self.content_object),
            content_object_str,
            str(getattr(self, 'user', False) or self.group),
            str(self.permission.codename))

    def save(self, *args, **kwargs):
        content_type = KobocatContentType.objects.get_for_model(
            self.content_object)
        if content_type != self.permission.content_type:
            raise BadContentTypeException(
                f"Cannot persist permission not designed for this "
                 "class (permission's type is {self.permission.content_type} "
                 "and object's type is {content_type}")
        return super().save(*args, **kwargs)


class KobocatUserPermission(ShadowModel):
    """ Needed to assign model-level KoBoCAT permissions """
    user = models.ForeignKey('KobocatUser', db_column='user_id',
                             on_delete=models.CASCADE)
    permission = models.ForeignKey('KobocatPermission',
                                   db_column='permission_id',
                                   on_delete=models.CASCADE)

    class Meta(ShadowModel.Meta):
        db_table = 'auth_user_user_permissions'


class KobocatUserProfile(ShadowModel):
    """
    From onadata/apps/main/models/user_profile.py
    Not read-only because we need write access to `require_auth`
    """
    class Meta(ShadowModel.Meta):
        db_table = 'main_userprofile'
        verbose_name = 'user profile'
        verbose_name_plural = 'user profiles'

    # This field is required.
    user = models.OneToOneField(KobocatUser,
                                related_name='profile',
                                on_delete=models.CASCADE)

    # Other fields here
    name = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=2, blank=True)
    organization = models.CharField(max_length=255, blank=True)
    home_page = models.CharField(max_length=255, blank=True)
    twitter = models.CharField(max_length=255, blank=True)
    description = models.CharField(max_length=255, blank=True)
    require_auth = models.BooleanField(
        default=False,
        verbose_name=_(
            "Require authentication to see forms and submit data"
        )
    )
    address = models.CharField(max_length=255, blank=True)
    phonenumber = models.CharField(max_length=30, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True,
                                   on_delete=models.CASCADE)
    num_of_submissions = models.IntegerField(default=0)
    metadata = JSONBField(default=dict, blank=True)


class KobocatToken(ShadowModel):

    key = models.CharField(_("Key"), max_length=40, primary_key=True)
    user = models.OneToOneField(KobocatUser,
                                related_name='auth_token',
                                on_delete=models.CASCADE, verbose_name=_("User"))
    created = models.DateTimeField(_("Created"), auto_now_add=True)

    class Meta(ShadowModel.Meta):
        db_table = "authtoken_token"

    @classmethod
    def sync(cls, auth_token):
        try:
            # Token use a One-to-One relationship on User.
            # Thus, we can retrieve tokens from users' id. 
            kc_auth_token = cls.objects.get(user_id=auth_token.user_id)
        except KobocatToken.DoesNotExist:
            kc_auth_token = cls(pk=auth_token.pk, user_id=auth_token.user_id)

        kc_auth_token.save()


class KobocatDigestPartial(ShadowModel):

    user = models.ForeignKey(KobocatUser, on_delete=models.CASCADE)
    login = models.CharField(max_length=128, db_index=True)
    partial_digest = models.CharField(max_length=100)
    confirmed = models.BooleanField(default=True)

    class Meta(ShadowModel.Meta):
        db_table = "django_digest_partialdigest"

    @classmethod
    def sync(cls, user):
        """
        Mimics the behavior of `django_digest.models._store_partial_digests()`,
        but updates `KobocatDigestPartial` in the KoBoCAT database instead of
        `PartialDigest` in the KPI database
        """
        cls.objects.filter(user=user).delete()
        # Query for `user_id` since user PKs are synchronized
        for partial_digest in PartialDigest.objects.filter(user_id=user.pk):
            cls.objects.create(
                user=user,
                login=partial_digest.login,
                confirmed=partial_digest.confirmed,
                partial_digest=partial_digest.partial_digest,
            )


def safe_kc_read(func):
    def _wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ProgrammingError as e:
            raise ProgrammingError('kc_access error accessing kobocat '
                                   'tables: {}'.format(e.message))
    return _wrapper
