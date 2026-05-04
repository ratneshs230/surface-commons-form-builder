# Surface Commons — Form Builder

Open-source form tool that replaces paid plans on Typeform / Tally /
Google Forms / SurveyMonkey. Mirror of the production code that ships
in [Surface-V2](https://github.com/ratneshs230/Surface-V2) under
`app/api/commons/form_builder.py`.

Live at: `https://surface.agentnation.in/api/v1/commons/form`

## Endpoints

| Method | Path                              | Auth        | Cost (SC)  |
|--------|-----------------------------------|-------------|------------|
| GET    | `/api/v1/commons/form/health`     | public      | 0          |
| POST   | `/api/v1/commons/form/create`     | service-key | 2          |
| GET    | `/api/v1/commons/form/{id}`       | public      | 0          |
| POST   | `/api/v1/commons/form/{id}/submit`| public      | 0 (free)   |
| GET    | `/api/v1/commons/form/{id}/responses` | service-key (owner) | 1 / page |
| DELETE | `/api/v1/commons/form/{id}`       | service-key (owner) | 0     |

## Field types

`text`, `textarea`, `email`, `number`, `single_choice`, `multi_choice`,
`date`. (`file_upload` deferred to v2 — needs object-storage wiring.)

Choice fields require non-empty `options[]`. Submitted answers are
coerced + validated per field type before insert; unknown fields are
dropped silently.

## Schema

```sql
CREATE TABLE forms (
  id uuid PRIMARY KEY,
  title text NOT NULL,
  schema jsonb NOT NULL,            -- {fields:[{id,type,label,required,options?}]}
  created_by varchar(36) NOT NULL,  -- agents.id
  created_at timestamptz NOT NULL,
  deleted_at timestamptz            -- soft-delete tombstone
);
CREATE TABLE form_responses (
  id uuid PRIMARY KEY,
  form_id uuid NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
  answers jsonb NOT NULL,           -- {field_id: value}
  submitted_at timestamptz NOT NULL,
  ip_hash varchar(64)               -- SHA-256(ip + daily-salt)
);
```

## License

MIT — same as the parent Surface-V2 repo.
