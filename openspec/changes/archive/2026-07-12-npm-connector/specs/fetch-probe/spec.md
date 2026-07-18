# fetch-probe: npm-probe

## ADDED Requirements

### Requirement: Probe npm-дистрибутива
Для npm probe SHALL выбирать tarball версии (единственный дистрибутив), отклоняя его permanent-результатом при `unpackedSize` либо размере скачанного файла больше настраиваемого лимита (default 30 МБ). Из tarball SHALL извлекаться: общее число файлов, счётчики расширений, признак `has_index_js` (`package/index.{js,cjs,mjs,node}`), README (`package/readme*` без учёта регистра, лимит 500 000 символов) — README сохраняется в описания версии с origin `tarball-readme`. Извлечение SHALL выполняться в памяти чтением известных имён, без полной распаковки.

#### Scenario: Обычный npm-пакет
- **WHEN** probe скачивает tarball с package/index.js и package/README.md
- **THEN** результат содержит `has_index_js = true`, README сохранён с origin `tarball-readme`

#### Scenario: Огромный tarball
- **WHEN** `dist.unpackedSize` версии превышает лимит
- **THEN** сохраняется permanent-результат с ошибкой без скачивания

#### Scenario: Повреждённый tarball
- **WHEN** скачанный файл не является корректным gzip/tar
- **THEN** сохраняется permanent-результат с ошибкой парсинга, задача не ретраится
