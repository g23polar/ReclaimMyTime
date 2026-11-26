import Foundation

/// `TodoKind`. `daily` and `weekly` are recurring checklists (ticked per period); `oneOff` is a
/// single item completed via `completedAt`. Fixed after create.
enum TodoKind: String, Codable, Hashable, Sendable {
    case daily, weekly
    case oneOff = "one_off"

    var isRecurring: Bool { self != .oneOff }
}

/// `Todo`. Decoding enforces the contract's kind-specific rules: a one-off has no period
/// completions, and a recurring todo has no `completedAt`.
struct Todo: Codable, Hashable, Sendable, Identifiable {
    let id: UUID
    let kind: TodoKind
    let title: String
    let notes: String?
    let dueDate: LocalDate?
    let position: Int
    let completedAt: Date?
    let completedPeriods: [LocalDate]
    let createdAt: Date

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case id, kind, title, notes, position
        case dueDate = "due_date"
        case completedAt = "completed_at"
        case completedPeriods = "completed_periods"
        case createdAt = "created_at"
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try rejectUnknownKeys(CodingKeys.self, from: decoder)
        id = try container.decode(UUID.self, forKey: .id)
        kind = try container.decode(TodoKind.self, forKey: .kind)
        title = try container.decode(String.self, forKey: .title)
        notes = try container.decodeIfPresent(String.self, forKey: .notes)
        dueDate = try container.decodeIfPresent(LocalDate.self, forKey: .dueDate)
        position = try container.decode(Int.self, forKey: .position)
        completedAt = try container.decodeIfPresent(Date.self, forKey: .completedAt)
        completedPeriods = try container.decode([LocalDate].self, forKey: .completedPeriods)
        createdAt = try container.decode(Date.self, forKey: .createdAt)
        try validateTitle(title, in: container, key: CodingKeys.title)
        try validateNotes(notes, in: container, key: CodingKeys.notes)
        if !kind.isRecurring, !completedPeriods.isEmpty {
            throw DecodingError.dataCorruptedError(forKey: .completedPeriods, in: container, debugDescription: "a one-off todo has no period completions")
        }
        if kind.isRecurring, completedAt != nil {
            throw DecodingError.dataCorruptedError(forKey: .completedAt, in: container, debugDescription: "a recurring todo is ticked per period, not completed_at")
        }
    }
}

/// `TodoList`: `periods` are the current daily and weekly period starts.
struct TodoList: Codable, Hashable, Sendable {
    let todos: [Todo]
    let periods: TodoPeriods
}

struct TodoPeriods: Codable, Hashable, Sendable {
    let daily: LocalDate
    let weekly: LocalDate
}

/// `TodoDraft`: request body of `POST /todos`. Optional fields are omitted when nil.
struct TodoDraft: Codable, Hashable, Sendable {
    var kind: TodoKind
    var title: String
    var notes: String?
    var dueDate: LocalDate?

    init(kind: TodoKind = .oneOff, title: String, notes: String? = nil, dueDate: LocalDate? = nil) {
        self.kind = kind
        self.title = title
        self.notes = notes
        self.dueDate = dueDate
    }

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case kind, title, notes
        case dueDate = "due_date"
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try rejectUnknownKeys(CodingKeys.self, from: decoder)
        kind = try container.decodeIfPresent(TodoKind.self, forKey: .kind) ?? .oneOff
        title = try container.decode(String.self, forKey: .title)
        notes = try container.decodeIfPresent(String.self, forKey: .notes)
        dueDate = try container.decodeIfPresent(LocalDate.self, forKey: .dueDate)
        try validateTitle(title, in: container, key: CodingKeys.title)
        try validateNotes(notes, in: container, key: CodingKeys.notes)
        if kind.isRecurring, dueDate != nil {
            throw DecodingError.dataCorruptedError(forKey: .dueDate, in: container, debugDescription: "due_date is for one-off todos only")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        // The server defaults kind to one_off, so a one-off is sent without it.
        if kind != .oneOff { try container.encode(kind, forKey: .kind) }
        try container.encode(title, forKey: .title)
        try container.encodeIfPresent(notes, forKey: .notes)
        try container.encodeIfPresent(dueDate, forKey: .dueDate)
    }
}

/// `TodoPatch`: partial update. Nil means "absent"; `.null` clears a nullable field. `kind` is not
/// patchable. The contract requires at least one field.
struct TodoPatch: Codable, Hashable, Sendable {
    var title: String?
    var notes: Nullable<String>?
    var dueDate: Nullable<LocalDate>?
    var done: Bool?

    init(
        title: String? = nil,
        notes: Nullable<String>? = nil,
        dueDate: Nullable<LocalDate>? = nil,
        done: Bool? = nil
    ) {
        self.title = title
        self.notes = notes
        self.dueDate = dueDate
        self.done = done
    }

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case title, notes, done
        case dueDate = "due_date"
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try rejectUnknownKeys(CodingKeys.self, from: decoder)
        title = try container.decodeIfPresent(String.self, forKey: .title)
        notes = try Self.nullable(String.self, in: container, key: .notes)
        dueDate = try Self.nullable(LocalDate.self, in: container, key: .dueDate)
        done = try container.decodeIfPresent(Bool.self, forKey: .done)
        if let title { try validateTitle(title, in: container, key: CodingKeys.title) }
        if case .value(let notes)? = notes { try validateNotes(notes, in: container, key: CodingKeys.notes) }
        if title == nil, notes == nil, dueDate == nil, done == nil {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "a todo patch needs at least one field"))
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(title, forKey: .title)
        try Self.encodeNullable(notes, in: &container, key: .notes) { $0 }
        try Self.encodeNullable(dueDate, in: &container, key: .dueDate) { $0 }
        try container.encodeIfPresent(done, forKey: .done)
    }

    private static func nullable<T: Codable & Hashable & Sendable>(
        _ type: T.Type,
        in container: KeyedDecodingContainer<CodingKeys>,
        key: CodingKeys
    ) throws -> Nullable<T>? {
        guard container.contains(key) else { return nil }
        if try container.decodeNil(forKey: key) { return .null }
        return .value(try container.decode(T.self, forKey: key))
    }

    private static func encodeNullable<T, Wire: Encodable>(
        _ value: Nullable<T>?,
        in container: inout KeyedEncodingContainer<CodingKeys>,
        key: CodingKeys,
        wire: (T) -> Wire
    ) throws {
        switch value {
        case .none: break
        case .some(.null): try container.encodeNil(forKey: key)
        case .some(.value(let wrapped)): try container.encode(wire(wrapped), forKey: key)
        }
    }
}

/// `TodoOrder`: request body of `PUT /todos/order`. `ids` must be a whole kind's id set; the
/// server checks that, this type only checks the no-duplicates rule from the schema.
struct TodoOrder: Codable, Hashable, Sendable {
    var kind: TodoKind
    var ids: [UUID]

    private enum CodingKeys: String, CodingKey, CaseIterable { case kind, ids }

    init(kind: TodoKind, ids: [UUID]) {
        self.kind = kind
        self.ids = ids
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        try rejectUnknownKeys(CodingKeys.self, from: decoder)
        kind = try container.decode(TodoKind.self, forKey: .kind)
        ids = try container.decode([UUID].self, forKey: .ids)
        if Set(ids).count != ids.count {
            throw DecodingError.dataCorruptedError(forKey: .ids, in: container, debugDescription: "ids must be unique")
        }
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(kind, forKey: .kind)
        try container.encode(ids.map(\.wireString), forKey: .ids)
    }
}

/// The contract's wire form for ids is lowercase (the server emits and documents lowercase);
/// `UUID.uuidString` is uppercase.
private extension UUID {
    var wireString: String { uuidString.lowercased() }
}

/// The contract sets `additionalProperties: false` on these objects, so an unknown key is an error.
/// Codable ignores unknown keys, and a keyed container's `allKeys` lists only the known ones, so the
/// object is decoded as a dictionary to see every key.
private func rejectUnknownKeys<Key: CodingKey & CaseIterable>(_ keys: Key.Type, from decoder: any Decoder) throws {
    let known = Set(keys.allCases.map(\.stringValue))
    let object = try [String: JSONValue](from: decoder)
    if let unknown = object.keys.sorted().first(where: { !known.contains($0) }) {
        throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "unknown field \(unknown)"))
    }
}

/// JSON Schema `maxLength` counts code points, so count unicode scalars, not grapheme clusters.
private func validateTitle<Key: CodingKey>(_ title: String, in container: KeyedDecodingContainer<Key>, key: Key) throws {
    let length = title.unicodeScalars.count
    guard (1...200).contains(length) else {
        throw DecodingError.dataCorruptedError(forKey: key, in: container, debugDescription: "title must be 1–200 characters")
    }
}

private func validateNotes<Key: CodingKey>(_ notes: String?, in container: KeyedDecodingContainer<Key>, key: Key) throws {
    guard let notes else { return }
    guard notes.unicodeScalars.count <= 2000 else {
        throw DecodingError.dataCorruptedError(forKey: key, in: container, debugDescription: "notes must be at most 2000 characters")
    }
}
