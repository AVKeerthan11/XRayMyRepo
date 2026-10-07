# Golden fixture v1

`repo/` is a deliberately tiny polyglot repository. `expected_cim.json` is the **hand-written** CIM v1
snapshot document a correct v1 extractor must produce for it, compared after `SnapshotDocument.canonical()`.
The document is grouped by directory for readability, not stored in canonical order.

The snapshot identity is fixed: repository `github.com/xraymyrepo/golden-fixture`, placeholder commit
`0123456789abcdef0123456789abcdef01234567`, extractor `1.0.0`. File `blob_sha`s are real git blob ids of the
files (LF line endings, enforced by `.gitattributes`) and are checked by `tests/test_golden.py`.

| Case | Where | Expected |
|---|---|---|
| absolute import | `backend/app/main.py` | IMPORTS → `routes.py` (the file, since `router` is a variable) |
| relative import | `backend/app/users/models.py` | IMPORTS → `db.py#Base`, rule `py.import.relative` |
| repeated imports | `models.py` (`sqlalchemy`, `sqlalchemy.orm`) | one edge, `occurrence_count: 2` |
| class, method, constructor | `users/service.py` | `UserService`, `.get` (method), `.__init__` (constructor) |
| inheritance | `User(Base)`, `Base(DeclarativeBase)`, `UserOut(BaseModel)` | internal and external INHERITS |
| FastAPI endpoint | `users/routes.py` | `endpoint:http:GET /users/{user_id}@pkg:pypi:acme-backend` (router prefix applied; scoped to the enclosing package) |
| ORM model / schema | `User` / `UserOut` | roles `orm_entity` / `schema` (classifications, not kinds) |
| test file / function | `backend/tests/test_users.py` | roles + TESTS → `UserService` (heuristic, medium) |
| unresolved import | `import acme_legacy_billing` | `not_found` |
| dynamic import | `importlib.import_module(name)` | `dynamic` |
| parse failure | `backend/app/legacy.py` | `extraction_status: failed` + `parse_error` issue |
| unsupported language | `scripts/deploy.sh` | `extraction_status: unsupported` |
| generated code | `backend/app/generated/user_pb2.py` | origin `generated`, still extracted |
| vendored code | `frontend/vendor/` | origin `vendored`, `excluded`, no symbols |
| docs | `README.md` | origin `docs`, `excluded` |
| TS import / type-only | `frontend/src/orders/handlers.ts` | IMPORTS with `is_type_only` |
| barrel file | `frontend/src/orders/index.ts` | re-export edges; `index.ts` import resolved *through* it (`via`) |
| interface, function | `types.ts#Order`, `handlers.ts#getOrder` | `type_kind: interface`, `function_kind: function` |
| Express handler | `app.get("/orders/:id", getOrder)` | `endpoint:http:GET /orders/{id}@pkg:npm:@acme/web`, HANDLES with derived evidence |
| manifests | `backend/pyproject.toml`, `frontend/package.json` | packages, REQUIRES (prod/dev), `SQLAlchemy` → `ext:pypi:sqlalchemy` |
| entrypoints | `main.py` (heuristic), `src/index.ts` (observed, `package.json#/main`) | role `entrypoint` |
| layers | none | no snapshot `layer` classifications: v1 defines no layer convention rules yet |
| coverage | snapshot `coverage` | `CALLS` not declared, so no CALLS edges and no claim of absence |

When the fixture changes, update `expected_cim.json` by hand in the same commit. The tests fail on any drift
in blob ids, spans or files.
