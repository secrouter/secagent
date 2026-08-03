// secagent-roslyn — emit a secagent-analysis/v1 report from a C# solution/project.
//
// Usage: secagent-roslyn <path-to-.sln-or-.csproj> [repo-root]
// Writes the JSON contract to stdout; diagnostics to stderr. Offline: does NOT restore
// (the project must already be restored / a NuGet cache mounted). See
// docs/design/heavy-analysis-pipeline.md.

using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Build.Locator;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.MSBuild;

// Must run before any MSBuild type is touched.
MSBuildLocator.RegisterDefaults();
return await Analyzer.Run(args);

static class Analyzer
{
    // Fully-qualified type/namespace names, no `global::`, with generic params.
    static readonly SymbolDisplayFormat Q = new(
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypesAndNamespaces,
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters);

    // Readable member signature, e.g. "int Count(int a)".
    static readonly SymbolDisplayFormat Sig = new(
        memberOptions: SymbolDisplayMemberOptions.IncludeParameters | SymbolDisplayMemberOptions.IncludeType,
        parameterOptions: SymbolDisplayParameterOptions.IncludeType | SymbolDisplayParameterOptions.IncludeName,
        miscellaneousOptions: SymbolDisplayMiscellaneousOptions.UseSpecialTypes);

    public static async Task<int> Run(string[] args)
    {
        if (args.Length < 1)
        {
            Console.Error.WriteLine("usage: secagent-roslyn <sln-or-csproj> [repo-root]");
            return 2;
        }
        var target = Path.GetFullPath(args[0]);
        var repoRoot = Path.GetFullPath(args.Length > 1 ? args[1] : Path.GetDirectoryName(target) ?? ".");

        var functions = new List<Fn>();
        var types = new List<Ty>();
        var calls = new List<Call>();
        var restored = true;

        using var ws = MSBuildWorkspace.Create();
        ws.WorkspaceFailed += (_, e) =>
        {
            if (e.Diagnostic.Kind == WorkspaceDiagnosticKind.Failure) restored = false;
            Console.Error.WriteLine(e.Diagnostic.Message);
        };

        var projects = new List<Project>();
        if (target.EndsWith(".sln", StringComparison.OrdinalIgnoreCase))
            projects.AddRange((await ws.OpenSolutionAsync(target)).Projects);
        else
            projects.Add(await ws.OpenProjectAsync(target));

        string Rel(string? p) => p is null ? "" : Path.GetRelativePath(repoRoot, p).Replace('\\', '/');
        static int Line(SyntaxNode n) => n.GetLocation().GetLineSpan().StartLinePosition.Line + 1;

        foreach (var project in projects.Where(p => p.Language == LanguageNames.CSharp))
        {
            var comp = await project.GetCompilationAsync();
            if (comp is null) continue;

            foreach (var tree in comp.SyntaxTrees)
            {
                var model = comp.GetSemanticModel(tree);
                var root = await tree.GetRootAsync();
                var file = Rel(tree.FilePath);

                foreach (var td in root.DescendantNodes().OfType<BaseTypeDeclarationSyntax>())
                {
                    if (model.GetDeclaredSymbol(td) is not INamedTypeSymbol s) continue;
                    var kind = s.IsRecord ? "record" : s.TypeKind switch
                    {
                        TypeKind.Interface => "interface",
                        TypeKind.Struct => "struct",
                        TypeKind.Enum => "enum",
                        _ => "class",
                    };
                    var bases = s.BaseType is { SpecialType: not SpecialType.System_Object } b
                        ? new List<string> { b.ToDisplayString(Q) } : new List<string>();
                    types.Add(new Ty(s.ToDisplayString(Q), kind, file, Line(td), bases,
                        s.Interfaces.Select(i => i.ToDisplayString(Q)).ToList()));
                }

                foreach (var node in root.DescendantNodes())
                {
                    var declared = node switch
                    {
                        MethodDeclarationSyntax or ConstructorDeclarationSyntax
                            or LocalFunctionStatementSyntax => model.GetDeclaredSymbol(node),
                        _ => null,
                    };
                    if (declared is IMethodSymbol m)
                    {
                        var owning = m.ContainingType?.ToDisplayString(Q) ?? "";
                        functions.Add(new Fn(m.Name, Qualify(owning, m.Name), m.ToDisplayString(Sig),
                            file, Line(node),
                            node is ConstructorDeclarationSyntax ? "constructor" : "method", owning));
                    }
                }

                foreach (var inv in root.DescendantNodes().OfType<InvocationExpressionSyntax>())
                {
                    if (model.GetSymbolInfo(inv).Symbol is not IMethodSymbol callee) continue;
                    var loc = callee.Locations.FirstOrDefault(l => l.IsInSource);
                    if (loc is null) continue;                      // library/BCL — intra-project only
                    var caller = EnclosingMethod(inv, model);
                    if (caller is null) continue;
                    var edgeKind = callee.IsVirtual || callee.IsAbstract || callee.IsOverride ? "virtual"
                        : callee.ContainingType?.TypeKind == TypeKind.Interface ? "interface" : "direct";
                    var cOwning = callee.ContainingType?.ToDisplayString(Q) ?? "";
                    calls.Add(new Call(caller, Qualify(cOwning, callee.Name),
                        Rel(loc.SourceTree?.FilePath), file, Line(inv), edgeKind));
                }
            }
        }

        var report = new Report("secagent-analysis/v1", "C#", "roslyn-msbuild",
            functions, types, calls, new Build("msbuild", restored, true));
        var opts = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
            DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        };
        Console.WriteLine(JsonSerializer.Serialize(report, opts));
        return 0;
    }

    static string Qualify(string owning, string name) => owning.Length > 0 ? $"{owning}.{name}" : name;

    static string? EnclosingMethod(SyntaxNode node, SemanticModel model)
    {
        for (var n = node.Parent; n is not null; n = n.Parent)
        {
            if (n is MethodDeclarationSyntax or ConstructorDeclarationSyntax
                or LocalFunctionStatementSyntax or AccessorDeclarationSyntax
                && model.GetDeclaredSymbol(n) is IMethodSymbol m)
            {
                var owning = m.ContainingType?.ToDisplayString(Q) ?? "";
                return Qualify(owning, m.Name);
            }
        }
        return null;
    }

    record Fn(string Name, string QualifiedName, string Signature, string File, int Line,
        string Kind, string OwningType);
    record Ty(string QualifiedName, string Kind, string File, int Line,
        List<string> Bases, List<string> Interfaces);
    record Call(string CallerQualified, string CalleeQualified, string CalleeFile, string File,
        int Line, string EdgeKind);
    record Build(string System, bool Restored, bool Offline);
    record Report(string Schema, string Language, string Backend,
        List<Fn> Functions, List<Ty> Types, List<Call> Calls, Build Build);
}
