type MetadataRecord = Record<string, unknown>;

type FieldDecoder = (value: unknown) => unknown;

const SUPPORTED_VERSION = "1.20.1";
const DECODER_REVISION = "minestom-1.20.1-v2";

type FieldRule = {
  decode: FieldDecoder;
  defaultValue?: unknown;
};

const DYE_COLORS = [
  "white",
  "orange",
  "magenta",
  "light_blue",
  "yellow",
  "lime",
  "pink",
  "gray",
  "light_gray",
  "cyan",
  "purple",
  "blue",
  "brown",
  "green",
  "red",
  "black"
] as const;

/**
 * Semantic metadata rules translated from Minestom's 1.20.1 entity metadata
 * hierarchy (Apache-2.0). minecraft-data remains authoritative for the
 * version-specific metadata index -> field-name mapping.
 *
 * Only fields with packed or otherwise non-obvious wire semantics need a
 * decoder. Already named scalar metadata remains available unchanged in the
 * sibling `metadata` object.
 */
const COMMON_FIELD_RULES: Record<string, FieldRule> = {
  shared_flags: { decode: decodeSharedFlags, defaultValue: 0 },
  living_entity_flags: { decode: decodeLivingEntityFlags, defaultValue: 0 },
  mob_flags: { decode: decodeMobFlags, defaultValue: 0 }
};

const ENTITY_FIELD_RULES: Record<string, Record<string, FieldRule>> = {
  sheep: {
    wool: { decode: decodeSheepWool, defaultValue: 0 }
  },
  player: {
    player_mode_customisation: { decode: decodePlayerSkinParts }
  },
  horse: {
    flags: { decode: decodeHorseFlags, defaultValue: 0 },
    type_variant: { decode: decodeHorseVariant, defaultValue: 0 }
  },
  bee: {
    flags: { decode: decodeBeeFlags, defaultValue: 0 }
  },
  fox: {
    flags: { decode: decodeFoxFlags, defaultValue: 0 }
  },
  bat: {
    flags: { decode: decodeBatFlags, defaultValue: 0 }
  },
  armor_stand: {
    client_flags: { decode: decodeArmorStandFlags, defaultValue: 0 }
  }
};

export type EntityMetadataDecodeResult = {
  available: boolean;
  minecraft_version: string | null;
  decoder_revision: string;
  semantic_source: {
    project: "Minestom";
    minecraft_version: "1.20.1";
    license: "Apache-2.0";
    reference: string;
  };
  decoded: MetadataRecord;
  recognized_fields: string[];
  explicit_fields: string[];
  defaulted_fields: string[];
  passthrough_fields: string[];
  unavailable_reason?: string;
};

/**
 * Decode only semantics known to match the connected Minecraft version.
 *
 * Mineflayer stores entity metadata as a sparse array: values equal to the
 * protocol default may never appear in the current session. `knownFieldNames`
 * comes from minecraft-data's versioned registry and lets semantic rules apply
 * a Minestom-derived default only when that field is valid for this entity.
 * The raw `metadata` record remains packet-backed and is never mutated.
 */
export function decodeEntityMetadata(
  minecraftVersion: string | null,
  entityName: string | null,
  metadata: MetadataRecord,
  knownFieldNames: readonly string[] = Object.keys(metadata)
): EntityMetadataDecodeResult {
  const base = {
    minecraft_version: minecraftVersion,
    decoder_revision: DECODER_REVISION,
    semantic_source: {
      project: "Minestom" as const,
      minecraft_version: SUPPORTED_VERSION as "1.20.1",
      license: "Apache-2.0" as const,
      reference: "https://github.com/emortalmc/minestom-ce"
    }
  };
  if (minecraftVersion !== SUPPORTED_VERSION) {
    return {
      ...base,
      available: false,
      decoded: {},
      recognized_fields: [],
      explicit_fields: [],
      defaulted_fields: [],
      passthrough_fields: Object.keys(metadata),
      unavailable_reason: `No semantic metadata rules are registered for Minecraft ${minecraftVersion ?? "unknown"}.`
    };
  }

  const entityRules = entityName ? ENTITY_FIELD_RULES[entityName] ?? {} : {};
  const decoded: MetadataRecord = {};
  const recognizedFields: string[] = [];
  const explicitFields: string[] = [];
  const defaultedFields: string[] = [];
  const passthroughFields: string[] = [];
  const knownFields = new Set(knownFieldNames);
  const fields = [
    ...knownFieldNames,
    ...Object.keys(metadata).filter((fieldName) => !knownFields.has(fieldName))
  ];
  for (const fieldName of fields) {
    const rule = entityRules[fieldName] ?? COMMON_FIELD_RULES[fieldName];
    const hasExplicitValue = Object.prototype.hasOwnProperty.call(metadata, fieldName);
    if (!rule) {
      if (hasExplicitValue) {
        passthroughFields.push(fieldName);
      }
      continue;
    }
    const hasDefault = Object.prototype.hasOwnProperty.call(rule, "defaultValue");
    if (!hasExplicitValue && !hasDefault) {
      continue;
    }
    const decodedValue = rule.decode(
      hasExplicitValue ? metadata[fieldName] : rule.defaultValue
    );
    if (decodedValue === undefined) {
      if (hasExplicitValue) {
        passthroughFields.push(fieldName);
      }
      continue;
    }
    decoded[fieldName] = decodedValue;
    recognizedFields.push(fieldName);
    if (hasExplicitValue) {
      explicitFields.push(fieldName);
    } else {
      defaultedFields.push(fieldName);
    }
  }
  return {
    ...base,
    available: true,
    decoded,
    recognized_fields: recognizedFields,
    explicit_fields: explicitFields,
    defaulted_fields: defaultedFields,
    passthrough_fields: passthroughFields
  };
}

function decodeSharedFlags(value: unknown): MetadataRecord | undefined {
  const flags = integerValue(value);
  if (flags === null) {
    return undefined;
  }
  return {
    on_fire: hasBit(flags, 0x01),
    crouching: hasBit(flags, 0x02),
    sprinting: hasBit(flags, 0x08),
    swimming: hasBit(flags, 0x10),
    invisible: hasBit(flags, 0x20),
    glowing: hasBit(flags, 0x40),
    elytra_flying: hasBit(flags, 0x80)
  };
}

function decodeLivingEntityFlags(value: unknown): MetadataRecord | undefined {
  const flags = integerValue(value);
  if (flags === null) {
    return undefined;
  }
  return {
    hand_active: hasBit(flags, 0x01),
    active_hand: hasBit(flags, 0x02) ? "off_hand" : "main_hand",
    in_riptide_spin_attack: hasBit(flags, 0x04)
  };
}

function decodeMobFlags(value: unknown): MetadataRecord | undefined {
  const flags = integerValue(value);
  if (flags === null) {
    return undefined;
  }
  return {
    no_ai: hasBit(flags, 0x01),
    left_handed: hasBit(flags, 0x02),
    aggressive: hasBit(flags, 0x04)
  };
}

function decodeSheepWool(value: unknown): MetadataRecord | undefined {
  const packed = integerValue(value);
  if (packed === null) {
    return undefined;
  }
  const colorId = packed & 0x0f;
  return {
    color_id: colorId,
    color: DYE_COLORS[colorId] ?? "unknown",
    is_sheared: hasBit(packed, 0x10)
  };
}

function decodePlayerSkinParts(value: unknown): MetadataRecord | undefined {
  const flags = integerValue(value);
  if (flags === null) {
    return undefined;
  }
  return {
    cape: hasBit(flags, 0x01),
    jacket: hasBit(flags, 0x02),
    left_sleeve: hasBit(flags, 0x04),
    right_sleeve: hasBit(flags, 0x08),
    left_pants_leg: hasBit(flags, 0x10),
    right_pants_leg: hasBit(flags, 0x20),
    hat: hasBit(flags, 0x40)
  };
}

function decodeHorseFlags(value: unknown): MetadataRecord | undefined {
  const flags = integerValue(value);
  if (flags === null) {
    return undefined;
  }
  return {
    tamed: hasBit(flags, 0x02),
    saddled: hasBit(flags, 0x04),
    bred: hasBit(flags, 0x08),
    eating: hasBit(flags, 0x10),
    rearing: hasBit(flags, 0x20),
    mouth_open: hasBit(flags, 0x40)
  };
}

function decodeHorseVariant(value: unknown): MetadataRecord | undefined {
  const packed = integerValue(value);
  if (packed === null) {
    return undefined;
  }
  const colors = [
    "white",
    "creamy",
    "chestnut",
    "brown",
    "black",
    "gray",
    "dark_brown"
  ];
  const markings = ["none", "white", "white_field", "white_dots", "black_dots"];
  const colorId = packed & 0xff;
  const markingId = (packed >> 8) & 0xff;
  return {
    color_id: colorId,
    color: colors[colorId] ?? "unknown",
    marking_id: markingId,
    marking: markings[markingId] ?? "unknown"
  };
}

function decodeBeeFlags(value: unknown): MetadataRecord | undefined {
  const flags = integerValue(value);
  if (flags === null) {
    return undefined;
  }
  return {
    angry: hasBit(flags, 0x02),
    has_stung: hasBit(flags, 0x04),
    has_nectar: hasBit(flags, 0x08)
  };
}

function decodeFoxFlags(value: unknown): MetadataRecord | undefined {
  const flags = integerValue(value);
  if (flags === null) {
    return undefined;
  }
  return {
    sitting: hasBit(flags, 0x01),
    crouching: hasBit(flags, 0x04),
    interested: hasBit(flags, 0x08),
    pouncing: hasBit(flags, 0x10),
    sleeping: hasBit(flags, 0x20),
    faceplanted: hasBit(flags, 0x40),
    defending: hasBit(flags, 0x80)
  };
}

function decodeBatFlags(value: unknown): MetadataRecord | undefined {
  const flags = integerValue(value);
  return flags === null ? undefined : { hanging: hasBit(flags, 0x01) };
}

function decodeArmorStandFlags(value: unknown): MetadataRecord | undefined {
  const flags = integerValue(value);
  if (flags === null) {
    return undefined;
  }
  return {
    small: hasBit(flags, 0x01),
    has_arms: hasBit(flags, 0x04),
    no_base_plate: hasBit(flags, 0x08),
    marker: hasBit(flags, 0x10)
  };
}

function hasBit(value: number, mask: number): boolean {
  return (value & mask) !== 0;
}

function integerValue(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}
