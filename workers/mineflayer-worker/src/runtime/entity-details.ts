import type { Bot } from "mineflayer";
import { decodeEntityMetadata } from "./entity-metadata-decoder.js";

type RuntimeEntity = Bot["entity"];

type RegistryEntityRecord = {
  id?: number;
  internalId?: number;
  name?: string;
  displayName?: string;
  type?: string;
  category?: string;
  width?: number;
  height?: number;
  metadataKeys?: unknown;
};

const MAX_METADATA_FIELDS = 32;
const MAX_COLLECTION_ITEMS = 8;
const MAX_OBJECT_FIELDS = 10;
const MAX_STRING_LENGTH = 240;

/**
 * Return bounded entity details backed by the connected server's entity packets
 * and Mineflayer's version-specific registry. No entity-specific assumptions are
 * made here: protocol metadata indices are named through registry metadataKeys.
 */
export function entityServerDetails(
  bot: Bot,
  entity: RuntimeEntity
): Record<string, unknown> {
  const entityTypeId =
    typeof entity.entityType === "number" && Number.isFinite(entity.entityType)
      ? entity.entityType
      : null;
  const registryEntity = registryEntityRecord(bot, entityTypeId);
  const metadataKeys = Array.isArray(registryEntity?.metadataKeys)
    ? registryEntity.metadataKeys.filter((key): key is string => typeof key === "string")
    : [];
  const metadata: Record<string, unknown> = {};
  const metadataValues = Array.isArray(entity.metadata) ? entity.metadata : [];
  const fieldCount = Math.min(
    Math.max(metadataKeys.length, metadataValues.length),
    MAX_METADATA_FIELDS
  );

  for (let index = 0; index < fieldCount; index += 1) {
    const value = metadataValues[index];
    if (value === undefined) {
      continue;
    }
    const safeValue = boundedJsonValue(value);
    if (safeValue === undefined) {
      continue;
    }
    metadata[metadataKeys[index] ?? `metadata_index_${index}`] = safeValue;
  }
  const registryName = registryEntity?.name ?? entity.name ?? null;
  const minecraftVersion =
    typeof bot.version === "string" && bot.version ? bot.version : null;
  const semanticMetadata = decodeEntityMetadata(
    minecraftVersion,
    registryName,
    metadata,
    metadataKeys
  );

  return {
    source: "minecraft_server_entity_packets_and_versioned_registry",
    minecraft_version: minecraftVersion,
    entity_type_id: entityTypeId,
    registry_name: registryName,
    registry_display_name: registryEntity?.displayName ?? entity.displayName ?? null,
    registry_type: registryEntity?.type ?? entity.type ?? null,
    registry_category: registryEntity?.category ?? entity.kind ?? null,
    kind: entity.kind ?? null,
    uuid: entity.uuid ?? null,
    username: entity.username ?? null,
    dimensions: {
      width: finiteNumber(entity.width ?? registryEntity?.width),
      height: finiteNumber(entity.height ?? registryEntity?.height)
    },
    on_ground: Boolean(entity.onGround),
    is_valid: Boolean(entity.isValid),
    health: finiteNumber(entity.health),
    velocity: vectorPayload(entity.velocity),
    equipment: equipmentPayload(entity),
    effects: effectsPayload(entity.effects),
    metadata_available: Object.keys(metadata).length > 0,
    metadata,
    metadata_decoded: semanticMetadata.decoded,
    metadata_decoder: {
      available: semanticMetadata.available,
      minecraft_version: semanticMetadata.minecraft_version,
      decoder_revision: semanticMetadata.decoder_revision,
      semantic_source: semanticMetadata.semantic_source,
      recognized_fields: semanticMetadata.recognized_fields,
      explicit_fields: semanticMetadata.explicit_fields,
      defaulted_fields: semanticMetadata.defaulted_fields,
      passthrough_fields: semanticMetadata.passthrough_fields,
      ...(semanticMetadata.unavailable_reason
        ? { unavailable_reason: semanticMetadata.unavailable_reason }
        : {})
    }
  };
}

/** Return changed named metadata fields without assigning task-level meaning. */
export function entityMetadataDelta(
  beforeDetails: Record<string, unknown>,
  afterDetails: Record<string, unknown>
): Record<string, { before: unknown; after: unknown }> {
  const before = recordValue(beforeDetails.metadata);
  const after = recordValue(afterDetails.metadata);
  const delta: Record<string, { before: unknown; after: unknown }> = {};
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (JSON.stringify(before[key]) === JSON.stringify(after[key])) {
      continue;
    }
    delta[key] = {
      before: before[key] ?? null,
      after: after[key] ?? null
    };
  }
  return delta;
}

function registryEntityRecord(
  bot: Bot,
  entityTypeId: number | null
): RegistryEntityRecord | null {
  if (entityTypeId === null) {
    return null;
  }
  const registry = bot.registry as unknown as {
    entities?: Record<number, RegistryEntityRecord>;
  };
  return registry.entities?.[entityTypeId] ?? null;
}

function equipmentPayload(entity: RuntimeEntity): Array<Record<string, unknown>> {
  const slotNames = ["main_hand", "feet", "legs", "chest", "head"];
  if (!Array.isArray(entity.equipment)) {
    return [];
  }
  return entity.equipment
    .slice(0, slotNames.length)
    .map((item, index) => ({
      slot: slotNames[index],
      item: item
        ? {
            name: item.name,
            count: item.count
          }
        : null
    }))
    .filter((row) => row.item !== null);
}

function effectsPayload(value: unknown): Array<Record<string, unknown>> {
  const rows = Array.isArray(value)
    ? value
    : value && typeof value === "object"
      ? Object.values(value)
      : [];
  return rows
    .filter((effect): effect is Record<string, unknown> =>
      Boolean(effect && typeof effect === "object")
    )
    .slice(0, MAX_COLLECTION_ITEMS)
    .map((effect) => ({
      id: effect.id ?? null,
      amplifier: effect.amplifier ?? null,
      duration: effect.duration ?? null
    }));
}

function boundedJsonValue(
  value: unknown,
  depth = 0,
  seen: Set<object> = new Set()
): unknown {
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "number"
  ) {
    return value;
  }
  if (typeof value === "bigint") {
    return value.toString();
  }
  if (typeof value === "string") {
    return value.slice(0, MAX_STRING_LENGTH);
  }
  if (typeof value !== "object" || depth >= 2 || seen.has(value)) {
    return undefined;
  }
  seen.add(value);
  if (Array.isArray(value)) {
    return value
      .slice(0, MAX_COLLECTION_ITEMS)
      .map((item) => boundedJsonValue(item, depth + 1, seen))
      .filter((item) => item !== undefined);
  }
  const vector = vectorPayload(value);
  if (vector !== null) {
    return vector;
  }
  const output: Record<string, unknown> = {};
  for (const [key, nested] of Object.entries(value).slice(0, MAX_OBJECT_FIELDS)) {
    const safeValue = boundedJsonValue(nested, depth + 1, seen);
    if (safeValue !== undefined) {
      output[key] = safeValue;
    }
  }
  return Object.keys(output).length > 0 ? output : undefined;
}

function vectorPayload(value: unknown): Record<string, number> | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const vector = value as { x?: unknown; y?: unknown; z?: unknown };
  if (
    typeof vector.x !== "number" ||
    typeof vector.y !== "number" ||
    typeof vector.z !== "number"
  ) {
    return null;
  }
  return {
    x: vector.x,
    y: vector.y,
    z: vector.z
  };
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
