import io
import sys
import time
from collections import deque
from datetime import datetime
from functools import partial
from typing import Optional, Iterable

from django.db import models
from django.db.models import QuerySet, Q
from opensearch_dsl.document import Document as DSLDocument, IndexMeta as DSLIndexMeta
from opensearchpy.helpers import bulk, parallel_bulk

from . import fields
from .apps import DODConfig
from .exceptions import ModelFieldNotMappedError
from .indices import Index
from .management.enums import OpensearchAction
from .search import Search
from .signals import post_index

model_field_class_to_field_class = {
    models.AutoField: fields.IntegerField,
    models.BigAutoField: fields.LongField,
    models.BigIntegerField: fields.LongField,
    models.BooleanField: fields.BooleanField,
    models.CharField: fields.TextField,
    models.DateField: fields.DateField,
    models.DateTimeField: fields.DateField,
    models.DecimalField: fields.DoubleField,
    models.EmailField: fields.TextField,
    models.FileField: fields.FileField,
    models.FilePathField: fields.KeywordField,
    models.FloatField: fields.DoubleField,
    models.ImageField: fields.FileField,
    models.IntegerField: fields.IntegerField,
    models.NullBooleanField: fields.BooleanField,
    models.PositiveIntegerField: fields.IntegerField,
    models.PositiveSmallIntegerField: fields.ShortField,
    models.SlugField: fields.KeywordField,
    models.SmallIntegerField: fields.ShortField,
    models.TextField: fields.TextField,
    models.TimeField: fields.LongField,
    models.URLField: fields.TextField,
    models.UUIDField: fields.KeywordField,
}


class IndexMeta(DSLIndexMeta):
    """A specialized DSL IndexMeta that specializes the Document Index class."""

    def __new__(mcs, *args, **kwargs):
        """Override `_index` with django_opensearch_dsl Index class."""
        new_cls = super().__new__(mcs, *args, **kwargs)
        if new_cls._index and new_cls._index._name:  # noqa
            new_cls._index.__class__ = Index  # noqa
        return new_cls


class Document(DSLDocument, metaclass=IndexMeta):
    """Allow the definition of Opensearch' index using Django `Model`."""

    VERSION_NAME_SEPARATOR = '--'
    _prepared_fields = []

    def __init__(self, related_instance_to_ignore=None, **kwargs):
        super(Document, self).__init__(**kwargs)
        # related instances to ignore is required to remove the instance
        # from related models on deletion.
        self._related_instance_to_ignore = related_instance_to_ignore
        self._prepared_fields = self.init_prepare()

    @classmethod
    def get_index_name(cls, suffix=None):
        """Compute the concrete Index name for the given (or not) suffix."""
        name = cls._index._name  # noqa
        if suffix:
            name += f'{cls.VERSION_NAME_SEPARATOR}{suffix}'
        return name

    @classmethod
    def get_all_indices(cls, using=None):
        """Fetches from OpenSearch all concrete indices for this Document."""
        return [
            Index(name)
            for name in sorted(
                cls._get_connection(using=using).indices.get(
                    f"{cls._index._name}{cls.VERSION_NAME_SEPARATOR}*"
                ).keys()
            )
        ]

    @classmethod
    def get_active_index(cls, using=None):
        """Return the Index that's active for this Document."""
        for index in cls.get_all_indices(using=using):
            if index.exists_alias(name=cls._index._name):  # noqa
                return index

    @classmethod
    def migrate(cls, suffix, using=None):
        """Sets an alias of the Document Index name to a given concrete Index."""
        index_name = cls.get_index_name(suffix)

        actions_on_aliases = [
            {"add": {"index": index_name, "alias": cls._index._name}},  # noqa
        ]

        active_index = cls.get_active_index()
        if active_index:
            actions_on_aliases.insert(
                0,
                {"remove": {"index": active_index._name, "alias": cls._index._name}},  # noqa
            )

        if len(actions_on_aliases) == 1 and cls._index.exists():
            cls._index.delete()

        cls._get_connection(using=using).indices.update_aliases(
            body={"actions": actions_on_aliases}
        )

    @classmethod
    def init(cls, suffix=None, using=None):
        """Init the Index with a named suffix to handle multiple versions.

        Create an alias to the default index name if it doesn't exist.
        """
        suffix = suffix or datetime.now().strftime("%Y%m%d%H%M%S%f")
        index_name = cls.get_index_name(suffix)
        super().init(index=index_name, using=using)
        if not cls._index.exists():
            cls.migrate(suffix, using=using)

    @classmethod
    def search(cls, using=None, index=None):
        """Return a `Search` object parametrized with the index' information."""
        return Search(
            using=cls._get_using(using), index=cls._default_index(index), doc_type=[cls], model=cls.django.model
        )

    def get_queryset(self, filter_: Optional[Q] = None, exclude: Optional[Q] = None, count: int = None) -> QuerySet:
        """Return the queryset that should be indexed by this doc type."""
        qs = self.django.model.objects.all()

        if filter_:
            qs = qs.filter(filter_)
        if exclude:
            qs = qs.exclude(exclude)
        if count is not None:
            qs = qs[:count]

        return qs

    def _eta(self, start, done, total):  # pragma: no cover
        if done == 0:
            return "~"
        eta = round((time.time() - start) / done * (total - done))
        unit = "secs"
        if eta > 120:
            eta //= 60
            unit = "mins"
        return f"{eta} {unit}"

    def get_indexing_queryset(
        self,
        verbose: bool = False,
        filter_: Optional[Q] = None,
        exclude: Optional[Q] = None,
        count: int = None,
        action: OpensearchAction = OpensearchAction.INDEX,
        stdout: io.FileIO = sys.stdout,
    ) -> Iterable:
        """Divide the queryset into chunks."""
        chunk_size = self.django.queryset_pagination
        qs = self.get_queryset(filter_=filter_, exclude=exclude, count=count)
        qs = qs.order_by("pk") if not qs.query.is_sliced else qs
        count = qs.count()
        model = self.django.model.__name__
        action = action.present_participle.title()

        i = 0
        done = 0
        start = time.time()
        if verbose:
            stdout.write(f"{action} {model}: 0% ({self._eta(start, done, count)})\r")
        while done < count:
            if verbose:
                stdout.write(f"{action} {model}: {round(i / count * 100)}% ({self._eta(start, done, count)})\r")

            for obj in qs[i : i + chunk_size]:
                done += 1
                yield obj

            i = min(i + chunk_size, count)

        if verbose:
            stdout.write(f"{action} {count} {model}: OK          \n")

    def init_prepare(self):
        """Initialise the data model preparers once here.

        Extracts the preparers from the model and generate a list of callables
        to avoid doing that work on every object instance over.
        """
        index_fields = getattr(self, "_fields", {})
        preparers = []
        for name, field in iter(index_fields.items()):
            if not isinstance(field, fields.DODField):  # pragma: no cover
                continue

            if not field._path:  # noqa
                field._path = [name]

            prep_func = getattr(self, "prepare_%s_with_related" % name, None)
            if prep_func:
                fn = partial(prep_func, related_to_ignore=self._related_instance_to_ignore)
            else:
                prep_func = getattr(self, "prepare_%s" % name, None)
                if prep_func:
                    fn = prep_func
                else:
                    fn = partial(field.get_value_from_instance, field_value_to_ignore=self._related_instance_to_ignore)

            preparers.append((name, field, fn))

        return preparers

    def prepare(self, instance):
        """Generate the opensearch's document from `instance` based on defined fields."""
        data = {name: prep_func(instance) for name, field, prep_func in self._prepared_fields}
        return data

    @classmethod
    def to_field(cls, field_name, model_field):
        """Return the opensearch field instance mapped to the model field class.

        This is a good place to hook into if you have more complex
        model field to OS field logic.
        """
        try:
            return model_field_class_to_field_class[model_field.__class__](attr=field_name)
        except KeyError:  # pragma: no cover
            raise ModelFieldNotMappedError(f"Cannot convert model field {field_name} to an Opensearch field!")

    def bulk(self, actions, using=None, **kwargs):
        """Execute given actions in bulk."""
        response = bulk(client=self._get_connection(using), actions=actions, **kwargs)
        # send post index signal
        post_index.send(sender=self.__class__, instance=self, actions=actions, response=response)
        return response

    def parallel_bulk(self, actions, using=None, **kwargs):
        """Parallel version of `bulk`."""
        kwargs.setdefault("chunk_size", self.django.queryset_pagination)
        bulk_actions = parallel_bulk(client=self._get_connection(using), actions=actions, **kwargs)
        # As the `parallel_bulk` is lazy, we need to get it into `deque` to run
        # it instantly.
        # See https://discuss.elastic.co/t/helpers-parallel-bulk-in-python-not-working/39498/2  # noqa
        deque(bulk_actions, maxlen=0)
        # Fake return value to emulate bulk() since we don't have a result yet,
        # the result is currently not used upstream anyway.
        return 1, []

    @classmethod
    def generate_id(cls, object_instance):
        """Generate the opensearch's _id from a Django `Model` instance.

        The default behavior is to use the Django object's pk (id) as the
        opensearch index id (_id). If needed, this method can be overloaded
        to change this default behavior.
        """
        return object_instance.pk

    def _prepare_action(self, object_instance, action, index_name=None):
        return {
            "_op_type": action,
            "_index": index_name or self._index._name,  # noqa
            "_id": self.generate_id(object_instance),
            "_source" if action != "update" else "doc": (self.prepare(object_instance) if action != "delete" else None),
        }

    def _get_actions(self, object_list, action, **kwargs):
        for object_instance in object_list:
            if action == "delete" or self.should_index_object(object_instance):
                yield self._prepare_action(object_instance, action, **kwargs)

    def _bulk(self, *args, parallel=False, using=None, **kwargs):
        """Helper for switching between normal and parallel bulk operation."""
        if parallel:
            return self.parallel_bulk(*args, using=using, **kwargs)
        return self.bulk(*args, using=using, **kwargs)

    def should_index_object(self, obj):
        """Whether given object should be indexed.

        Overwriting this method and returning a boolean value should determine
        whether the object should be indexed.
        """
        return True

    def update(self, thing, action, *args, index_suffix=None, refresh=None, using=None, **kwargs):  # noqa
        """Update document in OS for a model, iterable of models or queryset."""
        if refresh is None:
            refresh = getattr(self.Index, "auto_refresh", DODConfig.auto_refresh_enabled())

        index_name = self.__class__.get_index_name(index_suffix) if index_suffix else None

        if isinstance(thing, models.Model):
            object_list = [thing]
        else:
            object_list = thing

        return self._bulk(self._get_actions(object_list, action, index_name=index_name), *args, refresh=refresh, using=using, **kwargs)
