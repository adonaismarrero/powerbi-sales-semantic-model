// Validates the semantic model of this repository:
//   1. Loads the TMDL folder with the Tabular Object Model (fails on any syntax or reference error).
//   2. Checks that every 'Table'[Column] / [Measure] used in DAX exists in the model.
//   3. Applies a few modelling rules (formats, folders, hidden keys, relationship types).
// Usage: dotnet run --project tools/ModelValidator -- <path to the TMDL definition folder>

using System.Text.RegularExpressions;
using Microsoft.AnalysisServices.Tabular;

if (args.Length != 1)
{
    Console.Error.WriteLine("Usage: ModelValidator <path to the TMDL definition folder>");
    return 2;
}

Database database;
try
{
    database = TmdlSerializer.DeserializeDatabaseFromFolder(args[0]);
}
catch (Exception ex)
{
    Console.Error.WriteLine($"ERROR: the TMDL definition could not be loaded:\n{ex.Message}");
    return 1;
}

var model = database.Model;
var errors = new List<string>();
var warnings = new List<string>();

Console.WriteLine($"Model loaded (compatibility level {database.CompatibilityLevel})");
foreach (var table in model.Tables)
{
    var kind = table.CalculationGroup != null ? "calculation group" : "table";
    var columns = table.Columns.Count(c => c.Type != ColumnType.RowNumber);
    Console.WriteLine($"  {table.Name,-20} {kind,-18} {columns,3} columns {table.Measures.Count,3} measures");
}
Console.WriteLine($"  Relationships: {model.Relationships.Count}   Roles: {model.Roles.Count}   " +
                  $"Expressions: {model.Expressions.Count}");

// ---------------------------------------------------------------------------
// 1. DAX references
// ---------------------------------------------------------------------------
var tableNames = model.Tables.Select(t => t.Name).ToHashSet(StringComparer.OrdinalIgnoreCase);
var columnsByTable = model.Tables.ToDictionary(
    t => t.Name,
    t => t.Columns.Select(c => c.Name).ToHashSet(StringComparer.OrdinalIgnoreCase),
    StringComparer.OrdinalIgnoreCase);
var measureNames = model.Tables.SelectMany(t => t.Measures).Select(m => m.Name)
    .ToHashSet(StringComparer.OrdinalIgnoreCase);

var qualifiedRef = new Regex(@"(?:'(?<t>[^']+)'|(?<t>\b[A-Za-z_][A-Za-z0-9_]*))\[(?<c>[^\]]+)\]");
var bareRef = new Regex(@"(?<![\w'\]])\[(?<c>[^\]]+)\]");

static string StripLiterals(string dax) =>
    Regex.Replace(Regex.Replace(dax, "\"(?:[^\"]|\"\")*\"", "\"\""), @"//[^\n]*|--[^\n]*", "");

void CheckDax(string where, string? dax, string? ownTable)
{
    if (string.IsNullOrWhiteSpace(dax)) return;
    var code = StripLiterals(dax);
    foreach (Match m in qualifiedRef.Matches(code))
    {
        var t = m.Groups["t"].Value;
        var c = m.Groups["c"].Value;
        if (!tableNames.Contains(t))
            errors.Add($"{where}: unknown table '{t}' in {t}[{c}]");
        else if (!columnsByTable[t].Contains(c) && !measureNames.Contains(c))
            errors.Add($"{where}: '{t}'[{c}] is neither a column of '{t}' nor a measure");
    }
    foreach (Match m in bareRef.Matches(code))
    {
        var c = m.Groups["c"].Value;
        if (c.StartsWith('@')) continue;                                  // ADDCOLUMNS helper columns
        if (measureNames.Contains(c)) continue;
        if (ownTable != null && columnsByTable[ownTable].Contains(c)) continue;
        errors.Add($"{where}: [{c}] is not a measure" + (ownTable != null ? $" nor a column of '{ownTable}'" : ""));
    }
}

foreach (var table in model.Tables)
{
    foreach (var measure in table.Measures)
        CheckDax($"Measure [{measure.Name}]", measure.Expression, null);
    foreach (var column in table.Columns.OfType<CalculatedColumn>())
        CheckDax($"Calculated column '{table.Name}'[{column.Name}]", column.Expression, table.Name);
    if (table.CalculationGroup != null)
        foreach (var item in table.CalculationGroup.CalculationItems)
            CheckDax($"Calculation item '{item.Name}'", item.Expression, null);
}
foreach (var role in model.Roles)
    foreach (var permission in role.TablePermissions)
        CheckDax($"Role '{role.Name}' on '{permission.Table.Name}'", permission.FilterExpression, permission.Table.Name);

// ---------------------------------------------------------------------------
// 2. Modelling rules
// ---------------------------------------------------------------------------
foreach (var measure in model.Tables.SelectMany(t => t.Measures))
{
    if (string.IsNullOrWhiteSpace(measure.FormatString))
        errors.Add($"Measure [{measure.Name}] has no format string");
    if (string.IsNullOrWhiteSpace(measure.DisplayFolder))
        errors.Add($"Measure [{measure.Name}] has no display folder");
}

foreach (SingleColumnRelationship rel in model.Relationships.OfType<SingleColumnRelationship>())
{
    if (rel.FromColumn.DataType != rel.ToColumn.DataType)
        errors.Add($"Relationship {rel.Name}: data types differ " +
                   $"({rel.FromColumn.DataType} vs {rel.ToColumn.DataType})");
    if (!rel.FromColumn.IsHidden)
        warnings.Add($"Relationship {rel.Name}: foreign key {rel.FromTable.Name}[{rel.FromColumn.Name}] should be hidden");
    if (rel.CrossFilteringBehavior == CrossFilteringBehavior.BothDirections)
        warnings.Add($"Relationship {rel.Name}: bidirectional filtering");
}

var dateTables = model.Tables.Where(t => t.DataCategory == "Time").ToList();
if (dateTables.Count != 1)
    errors.Add($"Expected exactly one table marked as date table, found {dateTables.Count}");
else if (!dateTables[0].Columns.Any(c => c.IsKey && c.DataType == DataType.DateTime))
    errors.Add($"Date table '{dateTables[0].Name}' needs a key column of type DateTime");

if (!model.DiscourageImplicitMeasures)
    warnings.Add("DiscourageImplicitMeasures is off (required by calculation groups)");

// ---------------------------------------------------------------------------
foreach (var w in warnings) Console.WriteLine($"WARNING: {w}");
foreach (var e in errors) Console.Error.WriteLine($"ERROR: {e}");

var daxObjects = model.Tables.Sum(t => t.Measures.Count + t.Columns.OfType<CalculatedColumn>().Count()
                                       + (t.CalculationGroup?.CalculationItems.Count ?? 0));
Console.WriteLine(errors.Count == 0
    ? $"OK: {daxObjects} DAX expressions checked, {warnings.Count} warning(s)."
    : $"FAILED: {errors.Count} error(s).");
return errors.Count == 0 ? 0 : 1;
