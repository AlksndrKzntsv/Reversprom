#!/usr/bin/env bash
# Публикация сайта: синхронизирует папку site/ с бакетом Object Storage.
# В бакете остаётся ровно то, что лежит в site/ (лишние файлы удаляются).
#
#   ./deploy.sh --dryrun   — только показать изменения
#   ./deploy.sh            — загрузить
#
# Нужен AWS CLI с профилем reverseprom (настройка — docs/DEPLOY.md, раздел «Публикация сайта»).
set -euo pipefail
cd "$(dirname "$0")"

BUCKET="${BUCKET:-reverseprom.ru}"
PROFILE="${AWS_PROFILE:-reverseprom}"

if ! command -v aws >/dev/null; then
    echo "Не найден AWS CLI. Установите: brew install awscli" >&2
    exit 1
fi

# Публикуем то, что закоммичено: незакоммиченные правки в site/ легко потерять.
if [ -n "$(git status --porcelain -- site)" ]; then
    echo "В site/ есть незакоммиченные изменения:" >&2
    git status --short -- site >&2
    read -r -p "Всё равно опубликовать? [y/N] " answer
    [ "$answer" = "y" ] || exit 1
fi

aws s3 sync site/ "s3://$BUCKET/" \
    --endpoint-url=https://storage.yandexcloud.net \
    --profile "$PROFILE" \
    --delete \
    --exclude "*.DS_Store" \
    "$@"

echo "Готово: https://$BUCKET/"
