# Fenrus

`AllDebrid-Client` now exposes a compact Fenrus-friendly status endpoint:

- `/api/integrations/fenrus/status`

It returns:

- current queue/active/error state
- completed totals
- 24h / 7d completion counts
- success rate
- daily completion trend

## Smart App scaffold

A Fenrus Smart App scaffold is included here:

- [integrations/fenrus/AllDebridClient/README.md](/C:/Eigene%20Dateien/New%20project/integrations/fenrus/AllDebridClient/README.md)

Suggested flow:

1. Copy `integrations/fenrus/AllDebridClient` into Fenrus `Apps/Smart`.
2. Restart Fenrus.
3. Add the Smart App and set the base URL of your running `AllDebrid-Client`.

## Example

```text
http://alldebrid-client:8080/api/integrations/fenrus/status
```
