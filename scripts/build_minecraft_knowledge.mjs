#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import mcDataLoader from "../workers/mineflayer-worker/node_modules/minecraft-data/index.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/** Parse a small command-line option set without adding a dependency. */
function parseArgs(argv) {
  const args = {
    version: "1.20.1",
    output: path.join(ROOT, "knowledge", "processed", "minecraft_1_20_1_knowledge.json")
  };
  for (let index = 2; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--version") {
      args.version = argv[++index];
    } else if (arg === "--output") {
      args.output = argv[++index];
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return args;
}

/** Convert a Minecraft id into a natural-language alias. */
function labelFromName(name) {
  return name.replaceAll("_", " ");
}

/** Add a simple plural alias for common English labels. */
function plural(label) {
  if (label.endsWith("s")) return label;
  if (label.endsWith("y")) return `${label.slice(0, -1)}ies`;
  return `${label}s`;
}

/** Build aliases that help resolve task text without creating broad one-letter matches. */
function aliasesFor(name, displayName) {
  const label = labelFromName(name).toLowerCase();
  const aliases = new Set([name, label, displayName.toLowerCase()]);
  if (label.length > 2) aliases.add(plural(label));
  return [...aliases].filter((item) => item.length >= 2).sort();
}

/** Return an item name from an item id, falling back to block ids when needed. */
function nameForId(data, id) {
  if (id === null || id === undefined) return null;
  const item = data.items[id];
  if (item?.name) return item.name;
  const block = data.blocks[id];
  if (block?.name) return block.name;
  return null;
}

/** Extract ingredient ids from shaped or shapeless minecraft-data recipes. */
function recipeIngredientIds(recipe) {
  if (Array.isArray(recipe.ingredients)) {
    return recipe.ingredients.filter((id) => id !== null && id !== undefined);
  }
  if (Array.isArray(recipe.inShape)) {
    return recipe.inShape.flat().filter((id) => id !== null && id !== undefined);
  }
  return [];
}

/** Estimate whether a recipe needs a 3x3 crafting table instead of inventory crafting. */
function stationFor(recipe, ingredientIds) {
  if (Array.isArray(recipe.inShape)) {
    const height = recipe.inShape.length;
    const width = Math.max(...recipe.inShape.map((row) => row.length));
    return width > 2 || height > 2 ? "crafting_table" : "inventory";
  }
  return ingredientIds.length > 4 ? "crafting_table" : "inventory";
}

/** Count ingredient names for one representative recipe. */
function ingredientCounts(data, ingredientIds) {
  const counts = new Map();
  for (const id of ingredientIds) {
    const name = nameForId(data, id);
    if (!name) continue;
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([item_id, count]) => ({ item_id, count }));
}

/** Score recipe variants so common overworld materials become the default representative. */
function representativeScore(ingredients) {
  const names = ingredients.map((item) => item.item_id);
  let score = 0;
  for (const name of names) {
    if (name.startsWith("oak_")) score -= 10;
    if (name === "stick" || name === "iron_ingot" || name === "redstone" || name === "cobblestone") score -= 5;
    if (name.includes("warped") || name.includes("crimson") || name.includes("mangrove")) score += 4;
    if (name.includes("deepslate") || name.includes("nether")) score += 3;
  }
  score += ingredients.length;
  return score;
}

/** Build one recipe payload for a craftable output id. */
function buildRecipe(data, outputId, variants) {
  const outputItem = data.items[Number(outputId)] ?? data.blocks[Number(outputId)];
  if (!outputItem?.name) return null;
  const candidates = [];
  for (const recipe of variants) {
    const ingredientIds = recipeIngredientIds(recipe);
    if (!ingredientIds.length) continue;
    const ingredients = ingredientCounts(data, ingredientIds);
    if (!ingredients.length) continue;
    const resultCount = Number(recipe.result?.count ?? 1);
    candidates.push({
      output: outputItem.name,
      output_count: resultCount,
      station: stationFor(recipe, ingredientIds),
      ingredients,
      requires: stationFor(recipe, ingredientIds) === "crafting_table" ? ["crafting_table"] : [],
      description: `Craft ${resultCount} ${outputItem.name} from ${ingredients.map((item) => `${item.count} ${item.item_id}`).join(", ")}.`,
      alternative_count: variants.length
    });
  }
  if (!candidates.length) return null;
  candidates.sort((left, right) => representativeScore(left.ingredients) - representativeScore(right.ingredients));
  return candidates[0];
}

/** Build item, block, and entity terms from minecraft-data. */
function buildTerms(data) {
  const terms = [];
  const seen = new Set();
  function addTerm({ canonical_id, kind, name, aliases, description, tags }) {
    const key = `${kind}:${canonical_id}`;
    if (seen.has(key)) return;
    seen.add(key);
    terms.push({ canonical_id, kind, name, aliases, description, tags });
  }
  for (const item of data.itemsArray) {
    addTerm({
      canonical_id: item.name,
      kind: "item",
      name: item.displayName,
      aliases: aliasesFor(item.name, item.displayName),
      description: `Minecraft 1.20.1 item: ${item.displayName}.`,
      tags: ["minecraft_data", "item"]
    });
  }
  for (const block of data.blocksArray) {
    addTerm({
      canonical_id: block.name,
      kind: "block",
      name: block.displayName,
      aliases: aliasesFor(block.name, block.displayName),
      description: `Minecraft 1.20.1 block: ${block.displayName}.`,
      tags: ["minecraft_data", "block"]
    });
  }
  for (const entity of data.entitiesArray ?? []) {
    addTerm({
      canonical_id: entity.name,
      kind: "entity",
      name: entity.displayName,
      aliases: aliasesFor(entity.name, entity.displayName),
      description: `Minecraft 1.20.1 entity: ${entity.displayName}.`,
      tags: ["minecraft_data", "entity", String(entity.type ?? "entity")]
    });
  }
  return terms.sort((left, right) => `${left.kind}:${left.canonical_id}`.localeCompare(`${right.kind}:${right.canonical_id}`));
}

/** Build recipe payloads from minecraft-data's id-keyed recipe table. */
function buildRecipes(data) {
  return Object.entries(data.recipes)
    .map(([outputId, variants]) => buildRecipe(data, outputId, variants))
    .filter(Boolean)
    .sort((left, right) => left.output.localeCompare(right.output));
}

const args = parseArgs(process.argv);
const data = mcDataLoader(args.version);
const payload = {
  version: args.version,
  source: "minecraft-data",
  source_package_version: "3.111.0",
  terms: buildTerms(data),
  recipes: buildRecipes(data),
  documents: [
    {
      id: `minecraft-data-${args.version}-crafting-recipes`,
      title: `Minecraft ${args.version} Crafting Recipe Index`,
      tags: ["minecraft_data", "recipes", "crafting"],
      content: `Generated local recipe index from minecraft-data for Minecraft ${args.version}. Use get_recipe with canonical item ids such as compass, iron_pickaxe, furnace, crafting_table, or redstone_torch.`
    }
  ]
};

fs.mkdirSync(path.dirname(args.output), { recursive: true });
fs.writeFileSync(args.output, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
console.log(JSON.stringify({
  output: args.output,
  version: payload.version,
  term_count: payload.terms.length,
  recipe_count: payload.recipes.length
}, null, 2));
