# C# entrypoint-detection fixture

`Program.cs` is the first 34 lines of `src/Web/Program.cs` from eShopOnWeb
(https://github.com/dotnet-architecture/eShopOnWeb, MIT License, Copyright (c)
.NET Foundation and Contributors), vendored verbatim (the file is truncated
mid-statement; it is never compiled or parsed, only used by filename).

It exists for `tests/test_csharp_parity.py`, which pins the fix for a defect
found by measuring eShop with `secagent index`: the tool reported **zero**
entrypoints for a repo that has three (`src/Web/Program.cs`,
`src/PublicApi/Program.cs`, `src/BlazorAdmin/Program.cs`). All three use
.NET 6+ top-level statements — the default `dotnet new` template since 2021 —
which compile straight into an implicit `Main` with **no `Main` symbol at
all**. `project_map.py`'s entrypoint detection is two mechanisms: a filename
set (`_ENTRYPOINT_NAMES`) and a symbol regex
(`_ENTRYPOINT_SYMBOL_RE = ^main$|Main$`). C# was served by neither — no `Main`
symbol exists to match the regex, and `Program.cs` was not in the filename
set. This fixture is real top-level-statement C# (`using` directives, then
executable statements, no `Main` method, no enclosing class) so the test
exercises the actual shape that broke, not a synthetic stand-in for it.
