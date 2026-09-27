# Мониторинг инстансов PostgreSQL

Ноды инстанса PostgreSQL отправляют метрики в платформенную VictoriaMetrics
элемента `observability`. Пока этот элемент не установлен, ничего не
отправляется: vmagent базового образа ждёт, пока начнёт резолвиться
`victoria-storage.local.genesis-core.tech`, и только потом стартует.

Образ ноды должен быть собран на `exordos_base` 1.3.1 или новее — это первая
версия с vmagent и node_exporter. Ноды на старом образе получают их, когда их
переналивают из нового.

## Что собирается

vmagent опрашивает локальные эндпоинты, раз в 15 с, если не сказано иное:

| Job | Эндпоинт | Что покрывает |
|---|---|---|
| `node_exporter` | `127.0.0.1:9100`, из базового образа | ноду, включая диск данных |
| `patroni` | `127.0.0.1:8008/metrics`, REST API Patroni | роль, состояние HA, позиции WAL, таймлайн (`patroni_*`) |
| `postgres_exporter` | `127.0.0.1:9187`, `exordos-postgres-exporter` | сессии, блокировки, размеры баз, WAL, репликацию и слоты, checkpoint'ы, возраст ID транзакций (`pg_*`) |
| `postgres_exporter_databases` | `127.0.0.1:9188/probe`, `exordos-postgres-exporter-databases`, раз в 60 с | статистику таблиц каждой базы: размеры, строки, сканирования, vacuum (`pg_stat_user_tables_*`, `pg_statio_user_tables_*`, `pg_stat_progress_vacuum_*`), не больше 30000 серий на базу |

У каждой серии есть метки:

- `exordos_db_instance` — uuid инстанса;
- `exordos_project` — проект инстанса;
- `exordos_db_type` — движок, `postgres`: по ней дашборды и запросы каждого
  движка выбирают свои серии;
- `instance` — имя хоста ноды, `dbaas-dp-<uuid инстанса>-node-<суффикс>`.

У серий `postgres_exporter_databases` есть ещё `database` — опрошенная база, а
у потабличных — `datname`, `schemaname` и `relname`.

Patroni добавляет `scope` (имя инстанса) и `name` (uuid ноды; он же
`application_name` реплики на праймари).

Образ ноды заменяет шаблон конфигурации vmagent базового образа,
`/etc/exordos_observability/vmagent_scrape.yml.tpl`, в котором есть только
node_exporter, своим. Job'ы берут цели из file_sd-файлов, которые pg-агент
пишет вместе с метками: `/var/lib/exordos/exordos_db/vmagent_node.json` — для
job'ов ноды, `vmagent_databases.json` рядом — для баз. Проект control plane
передаёт агенту вместе с остальным ресурсом ноды. vmagent перечитывает файлы
раз в минуту, так что новая нода начинает опрашиваться в пределах минуты после
первого прохода агента, без перезапуска.

Почему два источника по PostgreSQL. Patroni отдаёт метрики с REST API, который
и так работает, поэтому они ничего не стоят, но описывают только кластер:
роль, позиции репликации, таймлайн, паузу. Для сессий, размеров баз и WAL
нужны запросы в PostgreSQL — их и делает postgres_exporter. Это пакет Ubuntu
(0.19.0 в 26.04), так что пинить отдельный бинарь не нужно, а работает он от
`postgres` через локальный сокет с peer-аутентификацией, так что и пароль
доставлять не нужно. Слушает только loopback.

Почему два postgres_exporter. Статистику таблиц PostgreSQL показывает только
сессии той же базы, а экспортер держит одно соединение. Первый, подключённый к
`postgres`, собирает статистику всего инстанса; второй сам ничего не собирает,
vmagent опрашивает его по разу на каждую базу, и в нём включены только
потабличные коллекторы и прогресс vacuum: имена таблиц разрешаются только в
базе сессии. Список баз pg-агент держит в
`/var/lib/exordos/exordos_db/vmagent_databases.json` — это file_sd-файл,
который vmagent перечитывает раз в минуту, так что новая база подхватывается
без перезапуска. Пишет его каждая нода: у реплики те же базы. Потабличная
статистика реплики учитывает только запросы на ней самой, дашборды берут
статистику праймари.

## Насколько заполнены диски

Диск данных смонтирован в `/persist` и bind-маунтами проброшен в
`/var/lib/postgresql/patroni/data` (PGDATA вместе с `pg_wal`),
`/var/lib/postgresql/patroni/raft` и `/var/log`, так что всё это занимает его
место, включая логи ноды. Так, как считает `df`, без блоков, зарезервированных
для root, по самой заполненной ноде каждого инстанса:

```promql
max by (exordos_db_instance) (
  100 * (
    node_filesystem_size_bytes{mountpoint="/var/lib/postgresql/patroni/data"}
    - node_filesystem_free_bytes{mountpoint="/var/lib/postgresql/patroni/data"}
  ) / (
    node_filesystem_size_bytes{mountpoint="/var/lib/postgresql/patroni/data"}
    - node_filesystem_free_bytes{mountpoint="/var/lib/postgresql/patroni/data"}
    + node_filesystem_avail_bytes{mountpoint="/var/lib/postgresql/patroni/data"}
  )
)
```

Ломается именно самая заполненная нода: PostgreSQL останавливается, как только
не может записать WAL. Ноды заполняются неравномерно: праймари держит WAL,
который ещё нужен слоту репликации отстающей или отвалившейся реплики, а
реплика держит WAL, который уже получила, но ещё не проиграла. Без `max`
видна каждая нода; то же выражение на интервале даёт историю, VictoriaMetrics
хранит её столько, сколько задано ретеншном элемента observability.

## Репликация

Отставание проигрывания каждой реплики в секундах, 0 — если она проиграла всё
полученное:

```promql
max by (exordos_db_instance) (pg_replication_lag_seconds)
```

Отставание проигрывания в байтах, как праймари видит каждую подключённую
реплику (`application_name` — uuid ноды реплики):

```promql
max by (exordos_db_instance, application_name) (pg_stat_replication_pg_wal_lsn_diff)
```

Реплика, потерявшая соединение, из запроса выше пропадает, а у Patroni
остаётся:

```promql
max by (exordos_db_instance) (patroni_xlog_location)
- on (exordos_db_instance) group_right ()
(patroni_xlog_replayed_location and patroni_replica == 1)
```

Число праймари; всё, кроме 1, значит, что у инстанса нет лидера или он
расщепился:

```promql
count by (exordos_db_instance) (patroni_primary == 1)
```

Смена таймлайна, `changes(patroni_postgres_timeline[1h])`, отмечает failover
или switchover.

## Сессии, базы и WAL

Доля занятых `max_connections` на каждой ноде, в процентах:

```promql
100 * sum by (exordos_db_instance, instance) (
  pg_stat_activity_count{backend_type="client backend"}
) / on (exordos_db_instance, instance)
max by (exordos_db_instance, instance) (pg_settings_max_connections)
```

Размеры баз:

```promql
max by (exordos_db_instance, datname) (pg_database_size_bytes{datname!~"template[01]"})
```

Размер `pg_wal` на каждой ноде:

```promql
max by (exordos_db_instance, instance) (pg_wal_size_bytes)
```

## Бэкапы

При включённых бэкапах таймер бэкапа (`exordos-db-pg-backup`, раз в 15 мин)
читает `pgbackrest info` на праймари и пишет найденное в
`/var/lib/exordos/node_exporter/exordos_db_backup.prom`, который отдаёт
node_exporter базового образа (его textfile collector, `exordos_base` 1.3.5 или
новее). Реплика и нода инстанса с выключенными бэкапами файл удаляют.
Серии приходят через job `node_exporter` с метками выше; бывший праймари держит
свой файл до следующего запуска, поэтому запросы берут только текущий праймари.

| Метрика | Метки | Смысл |
|---|---|---|
| `exordos_db_backup_start_timestamp_seconds`, `exordos_db_backup_stop_timestamp_seconds` | `backup_name`, `backup_type` | когда бэкап в репозитории начался и закончился |
| `exordos_db_backup_size_bytes` | `backup_name`, `backup_type` | размер базы в бэкапе |
| `exordos_db_backup_repo_delta_bytes` | `backup_name`, `backup_type` | сколько бэкап добавил в репозиторий, сжатым |
| `exordos_db_backup_error` | `backup_name`, `backup_type` | 1, если в бэкапе есть страницы с ошибками контрольных сумм; восстанавливается он всё равно |
| `exordos_db_backup_last_completion_timestamp_seconds` | `backup_type` | когда закончился последний бэкап этого типа; полный считается и `diff`, и `incr`, `diff` — ещё и `incr` |
| `exordos_db_backup_recovery_window_start_timestamp_seconds` | | самый ранний момент, до которого можно восстановиться, — конец самого старого бэкапа |
| `exordos_db_backup_archive_missing_segments` | | сегменты WAL, которых нет в архиве после последнего бэкапа; восстановиться дальше них можно только из более позднего бэкапа, его таймер и снимает |
| `exordos_db_backup_run_timestamp_seconds`, `exordos_db_backup_run_success` | | последний запуск таймера и удалось ли ему прочитать репозиторий и снять положенный бэкап |

Восстановиться можно от начала окна до последнего заархивированного WAL, то
есть на `pg_stat_archiver_last_archive_age` назад. PostgreSQL переключает
сегмент по `archive_timeout` (300 с), только если в него что-то писали, поэтому
на простаивающем инстансе возраст растёт, а ничего не теряется.

Время с последнего бэкапа любого типа, по текущему праймари:

```promql
time() - max by (exordos_db_instance) (
  exordos_db_backup_last_completion_timestamp_seconds{backup_type="incr"}
  and on (instance) (patroni_primary == 1)
)
```

Окно восстановления, в секундах:

```promql
max by (exordos_db_instance) (
  time() - pg_stat_archiver_last_archive_age
  - on (instance) group_left exordos_db_backup_recovery_window_start_timestamp_seconds
  and on (instance) (patroni_primary == 1)
)
```

Стоит алертить: нет бэкапа дольше интервала инкрементального плюс 15 мин
таймера, `exordos_db_backup_run_success == 0` дольше одного запуска,
`exordos_db_backup_archive_missing_segments > 0` и рост
`pg_stat_archiver_failed_count`.

## Логи

Базовый образ отправляет журнал каждой ноды в платформенную VictoriaLogs.
Patroni и PostgreSQL оба пишут в журнал `exordos-patroni` (PostgreSQL — через
stderr, с которым его запускает Patroni), так что их строки доходят без
какой-либо настройки на ноде. Имя хоста ноды в `_HOSTNAME` содержит uuid
инстанса:

```logsql
_HOSTNAME:~"^dbaas-dp-<uuid инстанса>-node-" _SYSTEMD_UNIT:"exordos-patroni.service"
```

Только ошибки:

```logsql
_HOSTNAME:~"^dbaas-dp-<uuid инстанса>-node-" _SYSTEMD_UNIT:"exordos-patroni.service" _msg:~"(ERROR|FATAL|PANIC):"
```

pgBackRest пишет собственные файлы в `/var/log/pgbackrest` (архивация WAL,
бэкапы, проверки); rsyslog на ноде читает их и отправляет строки в
VictoriaLogs как syslog с именем приложения `pgbackrest`. На ноде они тоже
остаются, ротация раз в неделю. Архивация и бэкапы идут на праймари:

```logsql
hostname:~"^dbaas-dp-<uuid инстанса>-node-" app_name:"pgbackrest"
```

Неудачные отправки WAL и бэкапы:

```logsql
hostname:~"^dbaas-dp-<uuid инстанса>-node-" app_name:"pgbackrest" _msg:~"(ERROR|WARN):"
```

## Транзакции, блокировки и обслуживание

Возраст самой старой открытой транзакции клиента по состояниям; `idle in
transaction` — клиент открыл транзакцию и не завершает её:

```promql
max by (exordos_db_instance, state) (
  pg_stat_activity_max_tx_duration{backend_type="client backend", state!="idle"}
)
```

Возраст ID транзакций как доля от `autovacuum_freeze_max_age`. После 1
autovacuum замораживает агрессивно; около 2^31 транзакций PostgreSQL
перестаёт принимать запись:

```promql
max by (exordos_db_instance) (
  max by (exordos_db_instance, instance) (pg_database_wraparound_age_datfrozenxid_seconds)
  / on (exordos_db_instance, instance)
  max by (exordos_db_instance, instance) (pg_settings_autovacuum_freeze_max_age)
)
```

WAL, который держит каждый слот репликации на праймари (копии слотов на
репликах Patroni держит неактивными):

```promql
max by (exordos_db_instance, slot_name) (
  pg_replication_slots_pg_wal_lsn_diff
  and on (instance) (patroni_primary == 1)
)
```

Мёртвые строки таблиц, на праймари:

```promql
sum by (exordos_db_instance, datname, schemaname, relname) (
  pg_stat_user_tables_n_dead_tup and on (instance) (patroni_primary == 1)
)
```

## Дашборды

Элемент `dbaas_dashboard` кладёт дашборды в папку **DBaaS** общей Grafana
элемента observability, с выбором проекта и инстанса. Зависит от элемента
`observability`, ставится после него.

- **PostgreSQL instance**: число праймари, роль каждой ноды, отставание
  репликации и слоты, самая старая транзакция, возраст ID транзакций,
  блокировки, deadlock'и и конфликты, сессии, транзакции, строки, временные
  файлы, checkpoint'ы, архивация WAL, бэкапы и окно восстановления, диски,
  CPU, память и сеть нод, основные настройки, а также логи Patroni и
  PostgreSQL с частотой ошибок.
- **PostgreSQL tables**: все таблицы выбранных баз с размером, строками,
  мёртвыми строками, сканированиями и последним autovacuum; самые большие
  таблицы, доля мёртвых строк, запись, последовательные сканирования,
  попадания в кэш и vacuum.

У каждого движка свои дашборды, метрики разные. Их uid —
`exordos-dbaas-<движок>-<вид>`, теги — `dbaas` и движок; ссылки в шапке ведут
на другие дашборды движка с выбранным инстансом и интервалом времени.

## Заметки

- **Нода старше метки проекта.** Её агент пропускает незнакомое поле и не
  сходится с target hash, пока ноду не перенальют с нового образа; агенты
  exordos_db до 2.5.0 на нём падают.
- **Шаблон — это вся конфигурация опроса ноды.** Он заменяет шаблон базового
  образа, поэтому job, который базовый образ добавит позже, здесь не
  опрашивается, пока его не добавят и в этот шаблон.
- **postgres_exporter читает под суперпользователем.** Peer-аутентификация
  сопоставляет системного пользователя `postgres` только с ролью `postgres`.
- **Каждый опрос базы открывает соединения.** Экспортер баз подключается
  заново при каждом опросе, так что при включённом `log_connections` каждая
  база добавляет в лог PostgreSQL несколько строк в минуту.
