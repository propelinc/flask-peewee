import cgi
import csv
import datetime
import functools
import operator
import json
import io as StringIO

from collections import defaultdict, deque
from flask import Blueprint, Response, request, url_for
from flask_login import current_user
from functools import reduce
from peewee import fn, DQ, DJANGO_MAP, OP, Expression, ModelAlias, Node
from peewee import DateField, DateTimeField, DoesNotExist, ForeignKeyField
from playhouse.postgres_ext import ArrayField, TSVectorField, JSONField
from werkzeug.datastructures import MultiDict

from .filters import make_field_tree
from .filters import PaginatedQuery
from .filters import get_object_or_404
from .filters import slugify

from .serializer import Deserializer
from .serializer import Serializer


DJANGO_MAP.update(
    contains=lambda l, r: Expression(l, '?', r),
    notin=lambda l, r: Expression(l, OP.NOT_IN, r),
)


class BadRequestException(Exception):
    """ Thrown if there is an error while parsing the request. """


class UserRequiredException(Exception):
    """ Thrown if a user needs to be provided to complete the request. """


class Authentication(object):
    def __init__(self, protected_methods=[]):
        self.protected_methods = protected_methods

    def authorize(self, resource):
        if request.method in self.protected_methods:
            return False

        return True


class AdminAuthentication(Authentication):
    def verify_user(self, user):
        return user.admin

    def authorize(self, resource):
        return (
            super(AdminAuthentication, self).authorize(resource)
            and current_user.is_authenticated
            and self.verify_user(current_user)
        )


class RestResource(object):
    paginate_by = 20

    escaped_fields = ()

    export_columns = []

    # serializing: dictionary of model -> field names to restrict output
    fields = None
    exclude = None

    # exclude certian fields from being exposed as filters -- for related fields
    # use "__" notation, e.g. user__password
    filter_exclude = None
    filter_fields = None
    filter_recursive = True

    # mapping of field name to resource class
    include_resources = None

    # delete behavior
    delete_recursive = True

    @classmethod
    def timestamp(cls, dt):
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    @property
    def user(self):
        if hasattr(self, '_user'):
            return self._user
        raise UserRequiredException

    def __init__(self, rest_api, model, authentication, allowed_methods=None):
        self.api = rest_api
        self.model = model
        self.pk = model._meta.primary_key

        self.authentication = authentication
        self.allowed_methods = allowed_methods or ['GET', 'POST', 'PUT', 'DELETE']

        self.aliases = defaultdict(dict)

        self._fields = {self.model: self.fields or list(self.model._meta.sorted_field_names)}
        if self.exclude:
            self._exclude = {self.model: self.exclude}
        else:
            self._exclude = {}

        self._filter_fields = self.filter_fields or list(self.model._meta.sorted_field_names)
        self._filter_exclude = self.filter_exclude or []

        self._resources = {}

        # recurse into nested resources
        if self.include_resources:
            for field_name, resource in self.include_resources.items():
                field_obj = self.model._meta.fields[field_name]
                resource_obj = resource(
                    self.api, field_obj.rel_model, self.authentication, self.allowed_methods)
                self._resources[field_name] = resource_obj
                self._fields.update(resource_obj._fields)
                self._exclude.update(resource_obj._exclude)

                self._filter_fields.extend([
                    '%s__%s' % (field_name, ff) for ff in resource_obj._filter_fields])
                self._filter_exclude.extend([
                    '%s__%s' % (field_name, ff) for ff in resource_obj._filter_exclude])

            self._include_foreign_keys = False
        else:
            self._include_foreign_keys = True

        self._field_tree = make_field_tree(
            self.model, self._filter_fields, self._filter_exclude, self.filter_recursive)

    def alias(self, model, fk):
        model_alias = model.alias()
        self.aliases[model][fk] = model_alias
        return model_alias

    def authorize(self):
        return self.authentication.authorize(self)

    def get_api_name(self):
        return slugify(self.model.__name__)

    def get_url_name(self, name):
        return '%s.%s_%s' % (
            self.api.blueprint.name,
            self.get_api_name(),
            name,
        )

    def get_query(self):
        return self.model.select()

    def process_datetime_arg(self, arg):
        try:
            return datetime.datetime.fromtimestamp(int(arg) // 1000)
        except (ValueError, TypeError):
            return arg

    def remove_dupes(self, lst):
        seen = set()
        for i in lst:
            if i not in seen:
                seen.add(i)
                yield i

    def filter_query(self, query, *args, **kwargs):
        # normalize args and kwargs into a new expression
        dq_node = Node()
        if args:
            dq_node &= reduce(operator.and_, [a.clone() for a in args])
        if kwargs:
            dq_node &= DQ(**kwargs)

        # dq_node should now be an Expression, lhs = Node(), rhs = ...
        q = deque([dq_node])
        dq_joins = list()
        while q:
            curr = q.popleft()
            if not isinstance(curr, Expression):
                continue
            for side, piece in (('lhs', curr.lhs), ('rhs', curr.rhs)):
                if isinstance(piece, DQ):
                    query_part, joins = self.convert_dict_to_node(piece.query)
                    dq_joins.extend(joins)
                    expression = reduce(operator.and_, query_part)
                    # Apply values from the DQ object.
                    expression._negated = piece._negated
                    expression._alias = piece._alias
                    setattr(curr, side, expression)
                else:
                    q.append(piece)

        dq_node = dq_node.rhs

        selected = list()
        query = query.clone()
        for field, rm in self.remove_dupes(dq_joins):
            selected.append(rm)
            if isinstance(field, ForeignKeyField):
                lm = field.model
                on = field
            if isinstance(on, ModelAlias):
                on = (rm == getattr(rm, rm._meta.primary_key.name))
            query = query.ensure_join(lm, rm, on)

        selected = self.remove_dupes(selected)
        if query._explicit_selection:
            query._select += query._model_shorthand(selected)
        else:
            selected.insert(0, query.model)
            query = query.select(*selected)

        return query.where(dq_node)

    def convert_dict_to_node(self, qdict):
        accum = []
        joins = []
        for key, value in sorted(qdict.items()):
            curr = self.model
            if '__' in key and key.rsplit('__', 1)[1] in DJANGO_MAP:
                key, op = key.rsplit('__', 1)
                op = DJANGO_MAP[op]
            else:
                op = OP.EQ
            for piece in key.split('__'):
                model_attr = getattr(curr, piece)
                if isinstance(model_attr, ForeignKeyField):
                    curr = model_attr.rel_model
                    if curr not in self.aliases:
                        self.aliases[curr] = {model_attr: curr}
                    elif model_attr not in self.aliases[curr]:
                        curr = self.alias(curr, model_attr)
                    else:
                        curr = self.aliases[curr][model_attr]
                    joins.append((model_attr, curr))
            accum.append(Expression(model_attr, op, value))
        return accum, joins

    def process_query(self, query, args=None):
        raw_filters = {}
        if args is None:
            args = MultiDict(request.json) if request.data else request.args.copy()

        # clean and normalize the request parameters
        for key in args:
            orig_key = key
            if key.startswith('-'):
                negated = True
                key = key[1:]
            else:
                negated = False
            if '__' in key:
                expr, op = key.rsplit('__', 1)
                if op not in DJANGO_MAP:
                    expr = key
                    op = 'eq'
            else:
                expr = key
                op = 'eq'

            if '.' in key:
                expr, lookups = expr.split('.', 1)
                lookups = lookups.split('.')
            else:
                lookups = ()

            # OP_IS implies that the value is None
            if op == 'is':
                raw_filters[expr] = [(op, [None], negated, lookups)]
            else:
                raw_filters.setdefault(expr, [])
                raw_filters[expr].append((op, args.getlist(orig_key), negated, lookups))

        # do a breadth first search across the field tree created by filter_fields,
        # searching for matching keys in the request parameters -- when found,
        # filter the query accordingly
        queue = [(self._field_tree, '')]
        while queue:
            node, prefix = queue.pop(0)
            for field in node.fields:
                filter_expr = '%s%s' % (prefix, field.name)
                if filter_expr in raw_filters:
                    for op, arg_list, negated, lookups in raw_filters[filter_expr]:
                        if isinstance(field, (DateField, DateTimeField)):
                            arg_list = [self.process_datetime_arg(arg) for arg in arg_list]
                            query = self.apply_filter(query, filter_expr, op, arg_list, negated)
                        elif isinstance(field, TSVectorField):
                            tsquery = '&'.join((arg.strip() for arg in arg_list[0].split(' ')))
                            expr = Expression(field, OP.TS_MATCH, fn.to_tsquery(tsquery))
                            expr = ~expr if negated else expr
                            query = query.where(expr)
                        elif isinstance(field, ArrayField):
                            expr = field.contains(arg_list)
                            expr = ~expr if negated else expr
                            query = query.where(expr)
                        elif isinstance(field, JSONField):
                            query = self.apply_json_filter(
                                query, field, op, arg_list, negated, lookups)
                        else:
                            query = self.apply_filter(
                                query, filter_expr, op, arg_list, negated)

            for child_prefix, child_node in node.children.items():
                queue.append((child_node, prefix + child_prefix + '__'))

        return query

    def construct_json_filter(self, lookup, op, value, negated):
        if op == 'contains':
            expr = lookup.contains(json.loads(value))
            return negated and ~expr or expr

        op = DJANGO_MAP[op]
        clause = op(lookup, str(value))
        return ~clause if negated else clause

    def apply_json_filter(self, query, field, op, arg_list, negated, lookups):
        json_lookup = field
        for lookup in lookups:
            json_lookup = json_lookup[lookup]

        if op in ('in', 'notin'):
            abs_expression = Expression(json_lookup, op, arg_list)
            expression = ~abs_expression if negated else abs_expression
            return query.where(expression)

        clauses = [self.construct_json_filter(json_lookup, op, v, negated) for v in arg_list]
        return query.where(reduce(operator.or_, clauses))

    def constructor(self, negated, kwargs):
        return ~DQ(**kwargs) if negated else DQ(**kwargs)

    def apply_filter(self, query, expr, op, arg_list, negated):
        query_expr = '%s__%s' % (expr, op)
        if op in ('in', 'notin'):
            return query.filter(self.constructor(negated, {query_expr: arg_list}))
        elif len(arg_list) == 1:
            return query.filter(self.constructor(negated, {query_expr: arg_list[0]}))
        else:
            query_clauses = [self.constructor(negated, {query_expr: val}) for val in arg_list]
            return query.filter(reduce(operator.or_, query_clauses))

    def get_serializer(self):
        return Serializer()

    def get_deserializer(self):
        return Deserializer()

    def prepare_data(self, obj, data):
        """
        Hook for modifying outgoing data
        """
        return data

    def serialize_object(self, obj):
        s = self.get_serializer()
        return self.prepare_data(
            obj, s.serialize_object(obj, self._fields, self._exclude)
        )

    def serialize_query(self, query):
        s = self.get_serializer()
        return [
            self.prepare_data(obj, s.serialize_object(obj, self._fields, self._exclude))
            for obj in query
        ]

    def deserialize_object(self, data, instance):
        d = self.get_deserializer()
        return d.deserialize_object(instance, data)

    def response_forbidden(self):
        return Response('Forbidden', 403)

    def response_bad_method(self):
        return Response('Unsupported method "%s"' % (request.method), 405)

    def response_bad_request(self):
        return Response('Bad request', 400)

    def response_api_exception(self, data, status=400):
        return Response(json.dumps(data), status, mimetype='application/json')

    def response_export(self, data, filename, mimetype):
        response = Response(data, mimetype=mimetype, content_type='application/octet-stream')
        response.headers['Content-Disposition'] = 'attachment; filename=%s' % filename
        return response

    def response(self, data):
        mimetype = request.args.get('mimetype') or 'application/json'
        data = {'status': 'OK'} if mimetype == 'text/html' else data
        res = Response(json.dumps(data), mimetype=mimetype)
        res.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        res.headers['Pragma'] = 'no-cache'
        res.headers['Expires'] = 0
        return res

    def require_method(self, func, methods):
        """ Overriden to add custom exception handling. """
        @functools.wraps(func)
        def inner(*args, **kwargs):
            if request.method not in methods:
                return self.response_bad_method()

            try:
                db = self.model._meta.database
                return db.atomic()(func)(*args, **kwargs)
            except DoesNotExist as err:
                return self.response_api_exception({'error': str(err)})
            except UserRequiredException:
                return self.response_api_exception({'error': 'user required'})
            except BadRequestException:
                return self.response_bad_request()
        return inner

    def get_urls(self):
        return (
            ('', self.require_method(self.api_list, ['GET', 'POST'])),
            ('/<pk>', self.require_method(self.api_detail, ['GET', 'POST', 'PUT', 'DELETE'])),
            ('/<pk>/delete', self.require_method(self.post_delete, ['POST', 'DELETE'])),
            ('/count', self.require_method(self.api_count, ['GET'])),
            ('/exportable', self.require_method(self.api_exportable, ['GET'])),
        )

    def check_get(self, obj=None):
        return True

    def check_post(self, obj=None):
        return True

    def check_put(self, obj):
        return True

    def check_delete(self, obj):
        return False

    def save_object(self, instance, raw_data):
        instance.save()
        return instance

    def api_list(self):
        if not getattr(self, 'check_%s' % request.method.lower())():
            return self.response_forbidden()

        if request.method == 'GET':
            return self.object_list()
        elif request.method == 'POST':
            return self.create()

    def api_detail(self, pk, method=None):
        obj = get_object_or_404(self.get_query(), self.pk == pk)

        method = method or request.method

        if not getattr(self, 'check_%s' % method.lower())(obj):
            return self.response_forbidden()

        if method == 'GET':
            return self.object_detail(obj)
        elif method in ('PUT', 'POST'):
            return self.edit(obj)
        elif method == 'DELETE':
            return self.delete(obj)

    def post_delete(self, pk):
        return self.api_detail(pk, 'DELETE')

    def api_count(self):
        if not getattr(self, 'check_%s' % request.method.lower())():
            return self.response_forbidden()

        query = self.get_query()
        query = self.apply_ordering(query)
        query = self.process_query(query)
        return self.response({'count': query.count()})

    def api_exportable(self):
        if not getattr(self, 'check_%s' % request.method.lower())():
            return self.response_forbidden()

        return self.response({
            'fields': [{
                'field': c,
                'name': h
            } for h, c, _ in self.export_columns]
        })

    def apply_ordering(self, query):
        ordering = request.args.get('ordering') or ''
        if ordering:
            desc, column = ordering.startswith('-'), ordering.lstrip('-')
            if column in self.model._meta.fields:
                field = self.model._meta.fields[column]
                query = query.order_by(field.asc() if not desc else field.desc())

        return query

    def get_request_metadata(self, paginated_query):
        var = paginated_query.page_var
        request_arguments = request.args.copy()

        current_page = paginated_query.get_page()
        next_page = previous_page = ''

        if current_page > 1:
            request_arguments[var] = current_page - 1
            previous_page = url_for(self.get_url_name('api_list'), **request_arguments)
        if current_page < paginated_query.get_pages():
            request_arguments[var] = current_page + 1
            next_page = url_for(self.get_url_name('api_list'), **request_arguments)

        return {
            'model': self.get_api_name(),
            'count': paginated_query.query.count(),
            'page': current_page,
            'previous': previous_page,
            'next': next_page,
        }

    def paginated_object_list(self, filtered_query):
        try:
            paginate_by = int(request.args.get('limit', self.paginate_by))
        except ValueError:
            paginate_by = self.paginate_by
        else:
            if self.paginate_by:
                paginate_by = min(paginate_by, self.paginate_by)  # restrict

        pq = PaginatedQuery(filtered_query, paginate_by)
        meta_data = self.get_request_metadata(pq)
        return self.send_objects(pq.get_list(), meta_data)

    def object_list(self):
        query = self.get_query()
        query = self.apply_ordering(query)

        # process any filters
        query = self.process_query(query)

        if self.paginate_by or 'limit' in request.args:
            return self.paginated_object_list(query)

        return self.send_objects(query)

    def object_detail(self, obj):
        return self.response(self.serialize_object(obj))

    def save_related_objects(self, instance, data):
        for k, v in data.items():
            if k in self._resources and isinstance(v, dict):
                rel_resource = self._resources[k]
                rel_obj, rel_models = rel_resource.deserialize_object(v, getattr(instance, k))
                rel_resource.save_related_objects(rel_obj, v)
                setattr(instance, k, rel_resource.save_object(rel_obj, v))

    def send_objects(self, objects, meta=None):
        output_format = request.args.get('format', None)
        fields = request.args.get('fields')

        if fields is not None:
            fields = fields.split(',')
            columns = [(h, k, v) for (h, k, v) in self.export_columns if k in fields]
        else:
            columns = self.export_columns

        if output_format == 'csv':
            return self.export_csv(objects, columns)
        elif output_format == 'xls':
            return self.export_xls(objects, columns)

        serialized = self.serialize_query(objects)
        response = {'meta': meta, 'objects': serialized} if meta else serialized
        return self.response(response)

    def extract_field(self, obj, field):
        try:
            for attr in field.split('.'):
                obj = getattr(obj, attr)
        except AttributeError:
            return None
        return obj

    def export_object(self, obj, columns):
        return {field: valtype(self.extract_field(obj, field)) for _, field, valtype in columns}

    def export_csv(self, objects, columns):
        csvdata = StringIO.StringIO()
        colfields = [c for _, c, _ in columns]
        w = csv.DictWriter(csvdata, colfields)
        w.writerow(dict((c, h) for h, c, _ in columns))
        w.writerows([self.export_object(obj, columns) for obj in objects])
        return self.response_export(csvdata.getvalue(), 'export.csv', 'text/csv')

    def export_xls(self, objects, columns):
        from xlsxwriter import Workbook
        xlsdata = StringIO.StringIO()
        book = Workbook(xlsdata)
        sheet = book.add_worksheet(self.model.__name__)

        colnames = [h for h, _, _ in columns]
        for c, h in enumerate(colnames):
            sheet.write(0, c, h)

        colfields = [c for _, c, _ in columns]
        data = (self.export_object(obj, columns) for obj in objects)
        for r, datum in enumerate(data):
            for c, f in enumerate(colfields):
                sheet.write(r + 1, c, datum.get(f, ''))

        book.close()
        return self.response_export(xlsdata.getvalue(), 'export.xlsx', 'application/msexcel')

    def read_request_data(self):
        data = request.data or request.form.get("data") or ""

        try:
            data = json.loads(data.decode())
        except ValueError:
            if not request.form:
                raise BadRequestException
            data = MultiDict(request.form)

        for k, v in data.items():
            if k in self.escaped_fields:
                data[k] = v and cgi.escape(v)

        return data

    def create(self):
        instance = self.create_object(self.read_request_data())
        return self.response(self.serialize_object(instance))

    def edit(self, obj):
        instance = self.edit_object(obj, self.read_request_data())
        return self.response(self.serialize_object(instance))

    def create_object(self, data):
        instance, models = self.deserialize_object(data, self.model())
        self.save_related_objects(instance, data)
        return self.save_object(instance, data)

    def edit_object(self, obj, data):
        obj, models = self.deserialize_object(data, obj)
        self.save_related_objects(obj, data)
        self.save_object(obj, data)
        return obj

    def delete(self, obj):
        res = obj.delete_instance(recursive=self.delete_recursive)
        return self.response({'deleted': res})


class ReadOnlyResource(RestResource):

    def check_post(self, obj=None):
        return False

    def check_put(self, obj):
        return False


class RestrictOwnerResource(RestResource):
    """ Restrict writes to owner of an object. """

    owner_field = 'user'

    def validate_owner(self, obj):
        user_id = getattr(current_user, "id", None)
        owner_id = getattr(obj, self.owner_field).id
        return user_id == owner_id

    def set_owner(self, obj, user):
        setattr(obj, self.owner_field, user)

    def check_post(self):
        return not current_user.is_anonymous

    def check_put(self, obj):
        return self.validate_owner(obj)

    def check_delete(self, obj):
        return self.validate_owner(obj)

    def save_object(self, instance, raw_data):
        self.set_owner(instance, current_user.id)
        return super(RestrictOwnerResource, self).save_object(instance, raw_data)


class RestAPI(object):
    def __init__(self, app, prefix='/api', default_auth=None, name='api'):
        self.app = app

        self._registry = {}

        self.url_prefix = prefix
        self.blueprint = self.get_blueprint(name)

        self.default_auth = default_auth or Authentication()

    def register(self, model, provider=RestResource, auth=None, allowed_methods=None):
        self._registry[model] = provider(self, model, auth or self.default_auth, allowed_methods)

    def unregister(self, model):
        del(self._registry[model])

    def is_registered(self, model):
        return self._registry.get(model)

    def auth_wrapper(self, func, provider):
        @functools.wraps(func)
        def inner(*args, **kwargs):
            if not provider.authorize():
                return Response({'error': 'Auth Failed'}, 401)
            return func(*args, **kwargs)
        return inner

    def get_blueprint(self, blueprint_name):
        return Blueprint(blueprint_name, __name__)

    def get_urls(self):
        return ()

    def configure_routes(self):
        for url, callback in self.get_urls():
            self.blueprint.route(url)(callback)

        for provider in self._registry.values():
            api_name = provider.get_api_name()
            for url, callback in provider.get_urls():
                full_url = '/%s%s' % (api_name, url)
                self.blueprint.add_url_rule(
                    full_url,
                    '%s_%s' % (api_name, callback.__name__),
                    self.auth_wrapper(callback, provider),
                    methods=provider.allowed_methods,
                    strict_slashes=True,
                )

    def register_blueprint(self, **kwargs):
        self.app.register_blueprint(self.blueprint, url_prefix=self.url_prefix, **kwargs)

    def setup(self):
        self.configure_routes()
        self.register_blueprint()
