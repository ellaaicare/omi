#if compiler(>=6.4)
import Foundation
import AppIntents
#if BASE_DEV_TIMED_CAPTURE
import ActivityKit
import Flutter
#endif

private func cleanedMemory(_ text: String) -> String {
    var result = text.trimmingCharacters(in: .whitespacesAndNewlines)
    if result.lowercased().hasPrefix("that ") { result = String(result.dropFirst(5)) }
    return result.trimmingCharacters(in: .whitespacesAndNewlines)
}

func cleanedSiriQuestion(_ text: String) -> String {
    var result = text.trimmingCharacters(in: .whitespacesAndNewlines)
    let prefixes = ["ask omi about ", "ask omi ", "ask about ", "ask "]
    if let prefix = prefixes.first(where: { result.lowercased().hasPrefix($0) }) {
        result = String(result.dropFirst(prefix.count)).trimmingCharacters(in: .whitespacesAndNewlines)
    }
    return result
}

private func requireSignedInSiriSession() throws {
    guard let config = SiriSession.shared.currentConfig() else { throw SiriSession.Failure.auth }
    try SiriSession.shared.validateOwner(config)
}

private func persistConfirmedWrite(owner: SiriSession.Config,
                                   _ operation: () async throws -> Void) async {
    do { try await operation() }
    catch {
        NSLog("[SiriIndex] Backend write confirmed; local snapshot repair scheduled")
        SiriTelemetry.index(outcome: "server", started: Date(), count: 0)
        SiriSnapshotStore.shared.scheduleConfirmedRepair(owner: owner)
    }
}

private func saveSiriMemory(_ value: String, owner: SiriSession.Config) async throws {
    let response = try await OmiNativeAPI().request(method: "POST", path: "/v3/memories", body: [
        "content": value, "category": "manual", "visibility": "private", "tags": ["siri"]
    ], owner: owner)
    guard let id = response["id"] as? String, !id.isEmpty else { throw SiriSession.Failure.server }
    await persistConfirmedWrite(owner: owner) {
        try await SiriSnapshotStore.shared.applyConfirmedMemory(
            id: id, content: response["content"] as? String ?? value, owner: owner)
    }
    SiriBridge.shared.memoryCreated(id)
}

/// Schema intents must return an entity on success. A typed LocalizedError
/// gives Siri a truthful spoken failure instead of returning a fake entity.
struct SiriSpokenError: LocalizedError {
    let failure: SiriSession.Failure
    let action: String
    let serverAction: String
    init(_ error: Error, action: String, serverAction: String) {
        failure = (error as? SiriSession.Failure) ?? .server
        self.action = action
        self.serverAction = serverAction
    }
    var errorDescription: String? {
        switch failure {
        case .auth, .invalidConfiguration: "Open Omi and sign in first."
        case .network: "I couldn't reach Omi, so \(action)."
        case .quota: "Your Omi limit has been reached, so \(action)."
        case .rateLimited: "Omi is receiving too many requests. Try again shortly."
        case .server: "Omi couldn't \(serverAction) right now."
        }
    }
}

private struct SiriUnsupportedInput: LocalizedError {
    enum Kind { case note, createTask, completeTask, open }
    let kind: Kind
    var errorDescription: String? {
        switch kind {
        case .note: "Omi can only save note text to Memories."
        case .createTask: "Omi can only create tasks with a title, due date, and the Omi list."
        case .completeTask: "Omi can only change task completion through Siri."
        case .open: "That item is no longer available in Omi."
        }
    }
}

@available(iOS 16.0, *)
struct RememberIntent: AppIntent {
    static var title: LocalizedStringResource = "Remember in Omi"
    static var description = IntentDescription("Save a private memory in Omi.")
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication
    static var openAppWhenRun: Bool = false
    @Parameter(title: "What should Omi remember?") var content: String

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let started = Date()
        var outcome = "server"
        defer { SiriTelemetry.intent("remember", outcome: outcome, started: started) }
        let value = cleanedMemory(content)
        guard !value.isEmpty else { outcome = "cancelled"; return .result(dialog: "What should Omi remember?") }
        do {
            guard let owner = SiriSession.shared.currentConfig() else { throw SiriSession.Failure.auth }
            try await saveSiriMemory(value, owner: owner)
            outcome = "ok"
            return .result(dialog: "Saved to Omi")
        } catch SiriSession.Failure.auth { outcome = "auth"; return .result(dialog: "Open Omi and sign in first.") }
        catch SiriSession.Failure.network { outcome = "network"; return .result(dialog: "I couldn't reach Omi, so nothing was saved.") }
        catch SiriSession.Failure.quota { outcome = "quota"; return .result(dialog: "Your Omi limit has been reached, so nothing was saved.") }
        catch SiriSession.Failure.rateLimited { outcome = "rateLimited"; return .result(dialog: "Omi is receiving too many requests. Try again shortly.") }
        catch { return .result(dialog: "Omi couldn't save that right now.") }
    }
}

/// This navigation helper is an output of AskOmiIntent and an app-internal
/// fallback. Keeping it out of discovery prevents Siri from treating its
/// optional draft as the primary one-breath question surface.
@available(iOS 16.0, *)
struct OpenOmiChatIntent: AppIntent {
    static var title: LocalizedStringResource = "Open Omi chat"
    static var description = IntentDescription("Open Omi's chat without asking a new question.")
    static var isDiscoverable = false
    static var openAppWhenRun = true
    @Parameter(title: "Draft") var draft: String?
    @Parameter(title: "Draft submission was attempted") var draftWasAttempted: Bool?
    @Parameter(title: "Owner") var ownerUID: String?
    @Parameter(title: "Owner generation") var ownerGeneration: String?

    static func shouldAutoSend(draft: String?, wasAttempted: Bool?) -> Bool {
        !cleanedSiriQuestion(draft ?? "").isEmpty && wasAttempted != true
    }

    static func route(draft: String?) throws -> String {
        guard let draft else { return "omi://chat" }
        var components = URLComponents()
        components.scheme = "omi"
        components.host = "chat"
        components.queryItems = [URLQueryItem(name: "draft", value: draft)]
        // URLComponents leaves a literal plus in a query value. Dart treats it
        // as a space, so encode it explicitly after URLQueryItem escapes the
        // other reserved characters.
        components.percentEncodedQuery = components.percentEncodedQuery?
            .replacingOccurrences(of: "+", with: "%2B")
        guard let route = components.string else { throw SiriSession.Failure.invalidConfiguration }
        return route
    }

    func perform() async throws -> some IntentResult {
        let started = Date()
        var outcome = "server"
        defer { SiriTelemetry.intent("openChat", outcome: outcome, started: started) }
        guard let config = SiriSession.shared.currentConfig(),
              ownerUID == nil || config.uid == ownerUID,
              ownerGeneration == nil || String(config.generation ?? 0) == ownerGeneration
        else {
            outcome = "auth"
            throw SiriSession.Failure.auth
        }
        try SiriSession.shared.validateOwner(config)
        let question = cleanedSiriQuestion(draft ?? "")
        guard !question.isEmpty else {
            SiriBridge.shared.navigate(try Self.route(draft: nil))
            outcome = "ok"
            return .result()
        }
        guard Self.shouldAutoSend(draft: draft, wasAttempted: draftWasAttempted) else {
            SiriBridge.shared.navigate(try Self.route(draft: question))
            outcome = "ok"
            return .result()
        }
        do {
            _ = try await OmiNativeAPI().ask(question: question, owner: config)
            SiriBridge.shared.navigate(try Self.route(draft: nil))
            outcome = "ok"
        } catch SiriSession.Failure.auth {
            outcome = "auth"
            throw SiriSession.Failure.auth
        } catch {
            outcome = SiriTelemetry.outcome(error)
            // Preserve the cleaned question when the one automatic send fails.
            SiriBridge.shared.navigate(try Self.route(draft: question))
        }
        return .result()
    }
}

/// Parameterless Siri target for opening the chat. The hidden continuation
/// above keeps its draft parameter out of Siri's question matching surface.
@available(iOS 16.0, *)
struct OpenOmiChatActionIntent: AppIntent {
    static var title: LocalizedStringResource = "Open Omi chat"
    static var description = IntentDescription("Open Omi chat to view or continue a conversation.")
    static var openAppWhenRun = true

    func perform() async throws -> some IntentResult {
        try await OpenOmiChatIntent().perform()
    }
}

@available(iOS 16.0, *)
struct AskOmiIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask Omi"
    static var description = IntentDescription(
        "Ask Omi a question about your conversations, memories, and tasks and get a spoken answer."
    )
    // Personal answers require an authenticated invoking device, including on lock screen.
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    static var openAppWhenRun = false
    static var parameterSummary: some ParameterSummary {
        Summary("Ask Omi \(\.$question)")
    }
    @Parameter(
        title: "Question",
        description: "A question for Omi to answer from your conversations, memories, and tasks.",
        requestValueDialog: "What would you like to ask Omi?"
    )
    var question: String

    static func fallbackOpenChat(_ openChat: OpenOmiChatIntent, question: String,
                                 didAttemptChatPost: Bool) -> OpenOmiChatIntent {
        openChat.draft = question
        openChat.draftWasAttempted = didAttemptChatPost
        return openChat
    }

    private static func offerChat(_ openChat: OpenOmiChatIntent,
                                  dialog: IntentDialog) async -> IntentResultContainer<Never, Never, Never, IntentDialog> {
        if #available(iOS 18.2, *) {
            return .result(opensIntent: openChat, dialog: dialog)
        }
        // The type-erased opensIntent result is unavailable on iOS 16–18.1.
        // Preserve the route so opening Omi manually still shows the draft.
        _ = try? await openChat.perform()
        return .result(dialog: dialog)
    }

    func perform() async throws -> IntentResultContainer<Never, Never, Never, IntentDialog> {
        let started = Date()
        var outcome = "server"
        defer { SiriTelemetry.intent("askOmi", outcome: outcome, started: started) }
        let performed = try await performReportingOutcome()
        outcome = performed.outcome
        return performed.result
    }

    /// Search delegates to Ask and must report the delegated failure class, so
    /// the flow returns its telemetry outcome alongside the spoken result.
    fileprivate func performReportingOutcome() async throws
        -> (result: IntentResultContainer<Never, Never, Never, IntentDialog>, outcome: String) {
        let value = cleanedSiriQuestion(question)
        let openChat = OpenOmiChatIntent()
        guard !value.isEmpty else {
            return (.result(dialog: "What would you like to ask Omi?"), "cancelled")
        }
        if Self.rememberRedirectDialog(for: value) != nil {
            return (.result(dialog: "To save that, say 'Remember something in Omi' instead."), "cancelled")
        }
        var didAttemptChatPost = false
        do {
            guard let owner = SiriSession.shared.currentConfig() else { throw SiriSession.Failure.auth }
            try SiriSession.shared.validateOwner(owner)
            openChat.ownerUID = owner.uid
            openChat.ownerGeneration = String(owner.generation ?? 0)
            didAttemptChatPost = true
            let answer = try await OmiNativeAPI().ask(question: value, owner: owner)
            // Short answers stay spoken-only; a truncated one must say where the
            // rest lives so the user keeps a route to the full text in chat.
            return (.result(dialog: IntentDialog("\(Self.spokenAnswer(answer))")), "ok")
        } catch SiriSession.Failure.auth {
            return (await Self.offerChat(openChat, dialog: "Open Omi and sign in first."), "auth")
        } catch SiriSession.Failure.network {
            return (await Self.offerChat(Self.fallbackOpenChat(openChat, question: value,
                                                               didAttemptChatPost: didAttemptChatPost),
                                        dialog: "Omi couldn't confirm the answer. Open Omi chat to check before trying again."),
                    "network")
        } catch {
            return (await Self.offerChat(Self.fallbackOpenChat(openChat, question: value,
                                                               didAttemptChatPost: didAttemptChatPost),
                                        dialog: "Omi couldn't confirm the answer. Open Omi chat to check before trying again."),
                    SiriTelemetry.outcome(error))
        }
    }

    /// Remember saves require local-device authentication (contracts/siri).
    /// Ask runs under the weaker companion-device `.requiresAuthentication`
    /// policy — an unlocked Watch can invoke it while the phone stays locked —
    /// so a remember phrase is redirected to the strict Remember shortcut
    /// instead of persisting a private memory under the weaker policy. No save
    /// is attempted here, so no success is claimed.
    static func rememberRedirectDialog(for question: String) -> String? {
        let lower = question.lowercased()
        guard lower.hasPrefix("remember ") || lower.hasPrefix("to remember ") else { return nil }
        return "To save that, say 'Remember something in Omi' instead."
    }

    /// The answer is also persisted to the owner's chat, so a truncated spoken
    /// answer must still say where the remainder can be read.
    static func spokenAnswer(_ answer: String) -> String {
        let trimmed = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count > 450 else { return trimmed }
        return String(trimmed.prefix(447)) + "… Open Omi chat for the rest."
    }
}

@available(iOS 27.0, *)
@AppIntent(schema: .notes.createNote)
struct OmiCreateNoteIntent {
    static var title: LocalizedStringResource = "Save a memory in Omi"
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication
    var name: String
    var content: AttributedString?
    var attachments: [IntentFile]
    var tags: [String]
    var isPinned: Bool
    var folder: OmiFolderEntity?
    init() {
        tags = []
        name = ""
        content = nil
        attachments = []
        isPinned = false
        folder = nil
    }
    func perform() async throws -> some ReturnsValue<ConversationEntity> & ProvidesDialog {
        let started = Date()
        do {
        let raw = content.map { String($0.characters) } ?? name
        let value = cleanedMemory(raw)
        guard !value.isEmpty, folder == nil || folder?.id == "memories",
              attachments.isEmpty, tags.isEmpty, !isPinned else {
            throw SiriUnsupportedInput(kind: .note)
        }
        guard let owner = SiriSession.shared.currentConfig() else { throw SiriSession.Failure.auth }
        let response = try await OmiNativeAPI().request(method: "POST", path: "/v3/memories", body: [
            "content": value, "category": "manual", "visibility": "private", "tags": ["siri"]
        ], owner: owner)
        guard let id = response["id"] as? String, !id.isEmpty else { throw SiriSession.Failure.server }
        await persistConfirmedWrite(owner: owner) {
            try await SiriSnapshotStore.shared.applyConfirmedMemory(
                id: id, content: response["content"] as? String ?? value, owner: owner)
        }
        SiriBridge.shared.memoryCreated(id)
        SiriTelemetry.intent("createNote", outcome: "ok", started: started)
        return .result(value: ConversationEntity(memoryId: id, content: value, creationDate: Date()), dialog: "Saved to Omi")
        } catch {
            SiriTelemetry.intent("createNote", outcome: SiriTelemetry.outcome(error), started: started)
            if let unsupported = error as? SiriUnsupportedInput { throw unsupported }
            throw SiriSpokenError(error, action: "the memory wasn't saved", serverAction: "save the memory")
        }
    }
}

@available(iOS 27.0, *)
@AppIntent(schema: .system.open)
struct OpenOmiIntent: OpenIntent {
    static var title: LocalizedStringResource = "Open in Omi"
    @Parameter(title: "Conversation") var target: ConversationEntity
    func perform() async throws -> some IntentResult {
        let started = Date()
        do { try requireSignedInSiriSession() }
        catch {
            SiriTelemetry.intent("open", outcome: SiriTelemetry.outcome(error), started: started,
                                 entryPath: "app_intent")
            throw error
        }
        let kind = target.folder?.id == "memories" ? "memory" : "conversation"
        #if OMI_SIRI_PROBE
        NSLog("[SiriSceneProbe] intent=open kind=%@ id=%@ guard=%@", kind, target.id,
              SiriSnapshotStore.shared.containsCurrentEntity(type: kind, id: target.id) ? "pass" : "reject")
        #endif
        guard SiriSnapshotStore.shared.containsCurrentEntity(type: kind, id: target.id) else {
            SiriTelemetry.intent("open", outcome: "server", started: started, entryPath: "app_intent")
            throw SiriUnsupportedInput(kind: .open)
        }
        guard SiriBridge.shared.navigate(SiriBridge.entityRoute(kind: kind, id: target.id),
                                         entryPath: "app_intent") else {
            throw SiriSession.Failure.auth
        }
        return .result()
    }
}

@available(iOS 27.0, *)
@AppIntent(schema: .system.open)
struct OpenOmiMemoryIntent: OpenIntent {
    static var title: LocalizedStringResource = "Open Omi memory"
    @Parameter(title: "Memory") var target: MemoryEntity
    func perform() async throws -> some IntentResult {
        let started = Date()
        do { try requireSignedInSiriSession() }
        catch {
            SiriTelemetry.intent("open", outcome: SiriTelemetry.outcome(error), started: started,
                                 entryPath: "app_intent")
            throw error
        }
        guard SiriSnapshotStore.shared.containsCurrentEntity(type: "memory", id: target.id) else {
            SiriTelemetry.intent("open", outcome: "server", started: started, entryPath: "app_intent")
            throw SiriUnsupportedInput(kind: .open)
        }
        guard SiriBridge.shared.navigate(SiriBridge.entityRoute(kind: "memory", id: target.id),
                                         entryPath: "app_intent") else {
            throw SiriSession.Failure.auth
        }
        return .result()
    }
}

@available(iOS 27.0, *)
@AppIntent(schema: .system.open)
struct OpenOmiTaskIntent: OpenIntent {
    static var title: LocalizedStringResource = "Open Omi task"
    @Parameter(title: "Task") var target: TaskEntity
    func perform() async throws -> some IntentResult {
        let started = Date()
        do { try requireSignedInSiriSession() }
        catch {
            SiriTelemetry.intent("open", outcome: SiriTelemetry.outcome(error), started: started,
                                 entryPath: "app_intent")
            throw error
        }
        guard SiriSnapshotStore.shared.containsCurrentEntity(type: "task", id: target.id) else {
            SiriTelemetry.intent("open", outcome: "server", started: started, entryPath: "app_intent")
            throw SiriUnsupportedInput(kind: .open)
        }
        guard SiriBridge.shared.navigate(SiriBridge.entityRoute(kind: "task", id: target.id),
                                         entryPath: "app_intent") else {
            throw SiriSession.Failure.auth
        }
        return .result()
    }
}

@available(iOS 27.0, *)
@AppIntent(schema: .system.open)
struct OpenOmiFolderIntent: OpenIntent {
    static var title: LocalizedStringResource = "Open Omi folder"
    @Parameter(title: "Folder") var target: OmiFolderEntity
    func perform() async throws -> some IntentResult {
        let started = Date()
        do { try requireSignedInSiriSession() }
        catch {
            SiriTelemetry.intent("open", outcome: SiriTelemetry.outcome(error), started: started,
                                 entryPath: "app_intent")
            throw error
        }
        guard target.id == "memories" || target.id == "conversations" else {
            SiriTelemetry.intent("open", outcome: "server", started: started, entryPath: "app_intent")
            throw SiriUnsupportedInput(kind: .open)
        }
        guard SiriBridge.shared.navigate(target.id == "memories" ? "omi://memories" : "omi://conversations",
                                         entryPath: "app_intent") else { throw SiriSession.Failure.auth }
        return .result()
    }
}

@available(iOS 27.0, *)
@AppIntent(schema: .system.open)
struct OpenOmiListIntent: OpenIntent {
    static var title: LocalizedStringResource = "Open Omi task list"
    @Parameter(title: "List") var target: OmiListEntity
    func perform() async throws -> some IntentResult {
        let started = Date()
        do { try requireSignedInSiriSession() }
        catch {
            SiriTelemetry.intent("open", outcome: SiriTelemetry.outcome(error), started: started,
                                 entryPath: "app_intent")
            throw error
        }
        guard target.id == "omi" else {
            SiriTelemetry.intent("open", outcome: "server", started: started, entryPath: "app_intent")
            throw SiriUnsupportedInput(kind: .open)
        }
        guard SiriBridge.shared.navigate("omi://action-items", entryPath: "app_intent") else {
            throw SiriSession.Failure.auth
        }
        return .result()
    }
}

@available(iOS 27.0, *)
@AppIntent(schema: .system.searchInApp)
struct SearchOmiIntent: ShowInAppSearchResultsIntent {
    static var searchScopes: [StringSearchScope] = [.general]
    static var title: LocalizedStringResource = "Search Omi and answer"
    // The system search schema requires local device authentication.
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication
    @Parameter(title: "Search") var criteria: StringSearchCriteria
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let started = Date()
        var outcome = "server"
        defer { SiriTelemetry.intent("search", outcome: outcome, started: started) }
        let ask = AskOmiIntent()
        ask.question = criteria.term
        // Ask catches its own failures and answers with a dialog, so the delegated
        // outcome — not the mere absence of a throw — is the Search result.
        // Delegating through performReportingOutcome skips AskOmiIntent.perform()'s
        // askOmi wrapper, so this entry point owns that event for the delegation.
        let askStarted = Date()
        var askOutcome = "server"
        defer { SiriTelemetry.intent("askOmi", outcome: askOutcome, started: askStarted) }
        let performed = try await ask.performReportingOutcome()
        askOutcome = performed.outcome
        outcome = performed.outcome
        return performed.result
    }
}

@available(iOS 27.0, *)
@AppIntent(schema: .reminders.updateReminder)
struct CompleteOmiTaskIntent {
    static var title: LocalizedStringResource = "Complete an Omi task"
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication
    var target: TaskEntity
    var title: String?
    var note: AttributedString?
    var tags: Set<String>?
    var urls: [URL]?
    var dueDate: DateComponents?
    var recurrence: Calendar.RecurrenceRule?
    var isCompleted: Bool?
    var isFlagged: Bool?
    var list: OmiListEntity?
    var locationTrigger: OmiLocationTriggerEntity?
    init() {
        target = TaskEntity(id: "", title: "", isCompleted: false,
                            creationDate: Date(), dueDate: nil, completionDate: nil)
        title = nil; note = nil; tags = nil; urls = nil; dueDate = nil
        recurrence = nil; isCompleted = nil; isFlagged = nil; list = nil
        locationTrigger = nil
    }
    func perform() async throws -> some ReturnsValue<TaskEntity> & ProvidesDialog {
        let started = Date()
        do {
        guard isCompleted == true, title == nil, note == nil, tags == nil, urls == nil,
              dueDate == nil, recurrence == nil, isFlagged == nil, list == nil, locationTrigger == nil
        else { throw SiriUnsupportedInput(kind: .completeTask) }
        let id = target.id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? target.id
        guard let owner = SiriSession.shared.currentConfig() else { throw SiriSession.Failure.auth }
        let response = try await OmiNativeAPI().request(method: "PATCH", path: "/v1/action-items/\(id)",
            body: ["completed": true], owner: owner)
        guard let returnedId = response["id"] as? String, returnedId == target.id else { throw SiriSession.Failure.server }
        await persistConfirmedWrite(owner: owner) {
            try await SiriSnapshotStore.shared.applyConfirmedTask(id: target.id,
                title: response["description"] as? String ?? target.title,
                completed: true, dueAt: target.dueDate.flatMap { Calendar.current.date(from: $0) }, owner: owner)
        }
        SiriBridge.shared.taskChanged(target.id)
        let entity = TaskEntity(id: target.id, title: target.title, isCompleted: true,
                                creationDate: target.creationDate ?? Date(),
                                dueDate: target.dueDate.flatMap { Calendar.current.date(from: $0) },
                                completionDate: Date())
        SiriTelemetry.intent("completeTask", outcome: "ok", started: started)
        return .result(value: entity, dialog: "Marked the task done in Omi.")
        } catch {
            SiriTelemetry.intent("completeTask", outcome: SiriTelemetry.outcome(error), started: started)
            if let unsupported = error as? SiriUnsupportedInput { throw unsupported }
            throw SiriSpokenError(error, action: "the task wasn't completed", serverAction: "complete the task")
        }
    }
}

@available(iOS 27.0, *)
@AppIntent(schema: .reminders.createReminder)
struct CreateOmiTaskIntent {
    static var title: LocalizedStringResource = "Create an Omi task"
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresLocalDeviceAuthentication
    var title: String
    var list: OmiListEntity?
    var note: AttributedString?
    var isFlagged: Bool?
    var images: [IntentFile]
    var tags: Set<String>
    var urls: [URL]
    var dueDate: DateComponents?
    var recurrence: Calendar.RecurrenceRule?
    var locationTrigger: OmiLocationTriggerEntity?
    var section: OmiSectionEntity?
    init() {
        title = ""; list = nil; note = nil; isFlagged = nil; images = []
        tags = []; urls = []; dueDate = nil; recurrence = nil
        locationTrigger = nil; section = nil
    }
    func perform() async throws -> some ReturnsValue<TaskEntity> & ProvidesDialog {
        let started = Date()
        do {
        let value = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, list == nil || list?.id == "omi",
              note.map({ String($0.characters).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) ?? true,
              isFlagged != true, images.isEmpty, tags.isEmpty, urls.isEmpty,
              recurrence == nil, locationTrigger == nil, section == nil else {
            throw SiriUnsupportedInput(kind: .createTask)
        }
        let due = dueDate.flatMap { Calendar.current.date(from: $0) }
        guard dueDate == nil || due != nil else { throw SiriUnsupportedInput(kind: .createTask) }
        var body: [String: Any] = ["description": value]
        if let due {
            body["due_at"] = ISO8601DateFormatter().string(from: due)
        }
        guard let owner = SiriSession.shared.currentConfig() else { throw SiriSession.Failure.auth }
        let response = try await OmiNativeAPI().request(method: "POST", path: "/v1/action-items",
            body: body, owner: owner)
        guard let id = response["id"] as? String, !id.isEmpty else { throw SiriSession.Failure.server }
        await persistConfirmedWrite(owner: owner) {
            try await SiriSnapshotStore.shared.applyConfirmedTask(id: id,
                title: response["description"] as? String ?? value,
                completed: false, dueAt: due, owner: owner)
        }
        SiriBridge.shared.taskChanged(id)
        let entity = TaskEntity(id: id, title: value, isCompleted: false, creationDate: Date(),
                                dueDate: due, completionDate: nil)
        SiriTelemetry.intent("createTask", outcome: "ok", started: started)
        return .result(value: entity, dialog: "Added the task to Omi.")
        } catch {
            SiriTelemetry.intent("createTask", outcome: SiriTelemetry.outcome(error), started: started)
            if let unsupported = error as? SiriUnsupportedInput { throw unsupported }
            throw SiriSpokenError(error, action: "the task wasn't created", serverAction: "create the task")
        }
    }
}

@available(iOS 16.0, *)
struct StartOmiListeningIntent: AppIntent {
    static var title: LocalizedStringResource = "Start listening with Omi"
    static var openAppWhenRun: Bool = true
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let started = Date()
        do {
            try requireSignedInSiriSession()
            try await SiriBridge.shared.setListening(true)
            SiriTelemetry.intent("startListening", outcome: "ok", started: started)
            return .result(dialog: "Omi is listening.")
        } catch SiriListeningFailure.deviceAlreadyListening {
            SiriTelemetry.intent("startListening", outcome: "ok", started: started)
            return .result(dialog: "Omi is already listening from your device.")
        } catch let failure as SiriListeningFailure {
            SiriTelemetry.intent("startListening", outcome: SiriTelemetry.outcome(failure), started: started)
            return .result(dialog: IntentDialog("\(failure.spokenDialog(starting: true))"))
        } catch SiriSession.Failure.auth {
            SiriTelemetry.intent("startListening", outcome: "auth", started: started)
            return .result(dialog: "Open Omi and sign in first.")
        } catch {
            SiriTelemetry.intent("startListening", outcome: SiriTelemetry.outcome(error), started: started)
            return .result(dialog: "Open Omi to start listening.")
        }
    }
}

@available(iOS 16.0, *)
struct StopOmiListeningIntent: AppIntent {
    static var title: LocalizedStringResource = "Stop listening with Omi"
    static var openAppWhenRun: Bool = true
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let started = Date()
        do {
            try requireSignedInSiriSession()
            try await SiriBridge.shared.setListening(false)
            SiriTelemetry.intent("stopListening", outcome: "ok", started: started)
            return .result(dialog: "Omi stopped listening.")
        } catch let failure as SiriListeningFailure {
            SiriTelemetry.intent("stopListening", outcome: SiriTelemetry.outcome(failure), started: started)
            return .result(dialog: IntentDialog("\(failure.spokenDialog(starting: false))"))
        } catch SiriSession.Failure.auth {
            SiriTelemetry.intent("stopListening", outcome: "auth", started: started)
            return .result(dialog: "Open Omi and sign in first.")
        } catch {
            SiriTelemetry.intent("stopListening", outcome: SiriTelemetry.outcome(error), started: started)
            return .result(dialog: "Open Omi to stop listening.")
        }
    }
}

/// UI donations carry only an opaque ID; no memory text, task title or transcript.
@available(iOS 26.0, *)
struct OmiUiActivityIntent: AppIntent {
    static var title: LocalizedStringResource = "Continue in Omi"
    static var supportedModes: IntentModes = .foreground(.immediate)
    @Parameter(title: "Item type") var kind: String
    @Parameter(title: "Item ID") var id: String

    func perform() async throws -> some IntentResult {
        let started = Date()
        var outcome = "server"
        defer { SiriTelemetry.intent("open", outcome: outcome, started: started) }
        do {
            // Fence authentication before the Boolean entity check collapses
            // Failure.auth into "no longer available", which would misclassify
            // the deferred outcome as server instead of auth.
            try requireSignedInSiriSession()
            guard SiriSnapshotStore.shared.containsCurrentEntity(type: kind, id: id) else {
                throw SiriUnsupportedInput(kind: .open)
            }
            let route = kind == "conversation" ? "omi://conversation/\(id)" :
                (kind == "memory" ? "omi://memory/\(id)" : "omi://task/\(id)")
            SiriBridge.shared.navigate(route)
            outcome = "ok"
            return .result()
        } catch {
            outcome = SiriTelemetry.outcome(error)
            throw error
        }
    }
}

@available(iOS 16.0, *)
struct OmiAppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: AskOmiIntent(), phrases: [
            "Ask \(.applicationName)",
            "Ask \(.applicationName) a question",
            "Question for \(.applicationName)",
            "\(.applicationName) question",
            "Check \(.applicationName)",
            "Ask a question in \(.applicationName)",
            "Ask \(.applicationName) something",
            "I have a question for \(.applicationName)"
        ], shortTitle: "Ask Omi", systemImageName: "bubble.left.and.text.bubble.right")
        AppShortcut(intent: OpenOmiChatActionIntent(), phrases: [
            "Open \(.applicationName) chat",
            "Open chat in \(.applicationName)"
        ], shortTitle: "Open Omi chat", systemImageName: "bubble.left.and.bubble.right")
        AppShortcut(intent: RememberIntent(), phrases: [
            "Remember something in \(.applicationName)",
            "Tell \(.applicationName) to remember",
            "Add a memory to \(.applicationName)"
        ], shortTitle: "Remember", systemImageName: "brain.head.profile")
        AppShortcut(intent: StartOmiListeningIntent(), phrases: ["Start listening with \(.applicationName)"],
                    shortTitle: "Start listening", systemImageName: "waveform")
        AppShortcut(intent: StopOmiListeningIntent(), phrases: ["Stop \(.applicationName)"],
                    shortTitle: "Stop listening", systemImageName: "stop.fill")
    }
}

@available(iOS 26.0, *)
extension RememberIntent {
    static var supportedModes: IntentModes { .background }
}

@available(iOS 26.0, *)
extension StartOmiListeningIntent {
    static var supportedModes: IntentModes { .foreground(.dynamic) }
}

@available(iOS 26.0, *)
extension StopOmiListeningIntent {
    static var supportedModes: IntentModes { .foreground(.dynamic) }
}
#if BASE_DEV_TIMED_CAPTURE
// DEV-ONLY Ella Base timed capture test (lib/dev/base_timed_capture.dart). Compiled only with the Swift
// condition BASE_DEV_TIMED_CAPTURE. No AppShortcut phrase: reachable only as a Shortcuts action. Starting the
// mic from the background is allowed only inside an AudioRecordingIntent perform, so the delayed start is
// this intent; Dart refuses unless armed (one shot, same account, time window).
//
// Exact ownership (v4): Dart asks for a token grant, then only the capture session it starts for the test (and
// that session's own restarts) mints session ids carrying the token (lib/dev/dev_mic_token.dart); ordinary starts
// never do. PhoneMicController refuses any activation of a revoked or expired token at every setup stage and never
// lets a test id adopt another session. The grant's expiry is enforced here, independent of Dart:
// at expiry the token is revoked and only a session carrying exactly that token is stopped and verified.
@available(iOS 18.0, *)
// v4.7: LiveActivityIntent, like the production OmiCaptureRecordingIntent. iOS lets Activity.request run from the
// background only inside a LiveActivityIntent; without it the capture card was refused (v4.6 token 461300147:
// native_live_activity_missing, then the app quit before its first audio chunk was written).
struct BaseTimedCaptureTestIntent: AudioRecordingIntent, LiveActivityIntent {
    static var title: LocalizedStringResource = "Run armed Ella Base timed capture test"
    static var description = IntentDescription(
        "Dev only. Records once, only if armed in Developer Settings in the last few minutes, then stops itself.")
    static var openAppWhenRun: Bool = false

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let outcome = try await BaseTimedCaptureDispatcher.run()
        return .result(dialog: IntentDialog(stringLiteral: "Timed capture test: \(outcome)"))
    }
}

@available(iOS 18.0, *)
enum BaseTimedCaptureDispatcher {
    /// Upper bound for a grant: startBound 15 s + max record 60 s + stopBound 10 s.
    static let maxGrant: TimeInterval = 85
    static let queryBound: TimeInterval = 2
    static let cutoffBound: TimeInterval = 5
    static let runBound: TimeInterval = 20
    static let liveActivityBound: TimeInterval = 3

    /// Set once on the main thread by AppDelegate, like OmiCaptureActionDispatcher.handler.
    static var channel: FlutterMethodChannel?
    static var mic: PhoneMicController?

    static func attach(messenger: FlutterBinaryMessenger, mic: PhoneMicController) {
        Self.mic = mic
        let channel = FlutterMethodChannel(name: "com.ella.base/dev_timed_capture", binaryMessenger: messenger)
        channel.setMethodCallHandler { call, result in Self.handleFromDart(call, result) }
        Self.channel = channel
    }

    // MARK: intent entry

    static func run() async throws -> String {
        let launchDeadline = Date().addingTimeInterval(10)  // cold background launch: wait for Flutter
        while Self.channel == nil || Self.mic == nil {
            if Date() >= launchDeadline { throw CaptureActionError.unavailable }
            try await Task.sleep(nanoseconds: 150_000_000)
        }
        await MainActor.run { LiveActivityManager.devLastStartError = nil }
        await Self.setLiveActivityWindow(runBound + liveActivityBound)
        var outcome = await Self.invokeDart("run", timeout: runBound) as? String ?? "dart_no_answer"
        if outcome.hasPrefix("recording") {
            let card = await Self.awaitLiveActivity()
            let startError = await MainActor.run { LiveActivityManager.devLastStartError }
            Self.evidence("native_live_activity_\(card.rawValue)", startError.map { ["start_error": $0] } ?? [:])
            // v4.7: never return from perform with an owned recording and no card. iOS ends such a recording
            // (and in v4.6 the process) without any stop line; stop it ourselves while we still can.
            if case .cutoff(let reason) = BaseTimedCaptureCardPolicy.afterStart(outcome: outcome, card: card.rawValue),
               let token = Self.liveToken {
                let result = await Self.cutoffOffMain(token, reason: reason)
                outcome = "cut_\(reason):\(result)"
            }
        }
        await Self.setLiveActivityWindow(nil)
        Self.evidence("native_trigger_outcome", ["outcome": outcome])
        return outcome
    }

    /// The token of the current grant (set on grant, cleared on revoke/expiry). Read from the intent's task.
    private static let liveTokenLock = NSLock()
    private static var _liveToken: Int64?
    static var liveToken: Int64? {
        get { liveTokenLock.lock(); defer { liveTokenLock.unlock() }; return _liveToken }
        set { liveTokenLock.lock(); _liveToken = newValue; liveTokenLock.unlock() }
    }

    /// cutoff() blocks on semaphores, so it must not run on the main thread or the intent's cooperative pool.
    private static func cutoffOffMain(_ token: Int64, reason: String) async -> String {
        await withCheckedContinuation { continuation in
            DispatchQueue.global().async { continuation.resume(returning: Self.cutoff(token, reason: reason)) }
        }
    }

    /// Apple: an AudioRecordingIntent must start a Live Activity when recording begins, or the recording stops.
    /// LiveActivityManager starts capture cards only in the foreground; this opens its dev window for this perform.
    private static func setLiveActivityWindow(_ seconds: TimeInterval?) async {
        await MainActor.run { LiveActivityManager.devIntentStartUntil = seconds.map { Date().addingTimeInterval($0) } }
    }

    private enum LiveActivityWait: String { case started, missing, waitCancelled = "wait_cancelled" }

    /// Bounded wait for the capture card LiveActivityManager starts from the recording's own snapshot.
    /// Cancelling the intent task ends only this wait (no busy loop): run() still closes the window and writes its
    /// evidence, and the recording keeps its owned stop (Dart devStopOwned, native grant expiry), never this task.
    private static func awaitLiveActivity() async -> LiveActivityWait {
        let deadline = Date().addingTimeInterval(liveActivityBound)
        while Date() < deadline {
            if Activity<OmiCaptureAttributes>.activities.contains(where: { $0.activityState == .active }) { return .started }
            do { try await Task.sleep(nanoseconds: 150_000_000) } catch { return .waitCancelled }
        }
        return .missing
    }

    // MARK: Dart -> native

    private static func token(_ call: FlutterMethodCall) -> Int64? {
        ((call.arguments as? [String: Any])?["token"] as? NSNumber)?.int64Value
    }

    private static func handleFromDart(_ call: FlutterMethodCall, _ result: @escaping FlutterResult) {
        switch call.method {
        case "grantToken":
            guard let token = Self.token(call),
                  let ttlMs = ((call.arguments as? [String: Any])?["ttl_ms"] as? NSNumber)?.doubleValue else {
                result(false); return
            }
            let ttl = min(max(ttlMs / 1000, 1), maxGrant)
            let now = Date()
            let granted = PhoneMicController.devTokens.grant(token, until: now.addingTimeInterval(ttl), now: now)
            Self.evidence(granted ? "native_token_granted" : "native_token_refused", ["token": token, "ttl_s": ttl])
            if granted {
                Self.liveToken = token
                DispatchQueue.global().asyncAfter(deadline: .now() + ttl) { Self.expire(token) }
            }
            result(granted)
        case "revokeToken":
            if let token = Self.token(call) {
                PhoneMicController.devTokens.revoke(token)
                if Self.liveToken == token { Self.liveToken = nil }
                Self.evidence("native_token_revoked", ["token": token])
            }
            result(nil)
        case "micOpen":
            guard let token = Self.token(call) else { result(nil); return }
            DispatchQueue.global().async {
                let open = Self.sessionOpen(token)
                DispatchQueue.main.async { result(open) }
            }
        case "cutoff":
            guard let token = Self.token(call) else { result("unverified"); return }
            DispatchQueue.global().async {
                let outcome = Self.cutoff(token, reason: "dart_escalation")
                DispatchQueue.main.async { result(outcome) }
            }
        case "terminal":
            Self.evidence("native_dart_terminal", ["status": (call.arguments as? [String: Any])?["status"] ?? "?"])
            result(nil)
        default:
            result(FlutterMethodNotImplemented)
        }
    }

    // MARK: expiry and cutoff (off the main thread)

    private static func expire(_ token: Int64) {
        PhoneMicController.devTokens.revoke(token)
        if Self.liveToken == token { Self.liveToken = nil }
        Self.evidence("native_token_expired", ["token": token])
        _ = Self.cutoff(token, reason: "native_deadline")
    }

    /// Revokes [token]; stops only a session carrying exactly [token]; verifies it is gone within cutoffBound.
    /// Returns "idle" | "not_owned" | "closed" | "unverified". Must not run on the main thread.
    static func cutoff(_ token: Int64, reason: String) -> String {
        guard let mic = Self.mic else {
            Self.evidence("UNVERIFIED_HOLD", ["token": token, "reason": reason, "detail": "no_controller"])
            return "unverified"
        }
        let box = StringBox()
        let done = DispatchSemaphore(value: 0)
        mic.devCutoff(token: token) { box.set($0); done.signal() }
        guard done.wait(timeout: .now() + cutoffBound) == .success, let decision = box.get() else {
            Self.evidence("UNVERIFIED_HOLD", ["token": token, "reason": reason, "detail": "controller_unresponsive"])
            return "unverified"
        }
        switch decision {
        case "idle", "not_owned":
            Self.evidence("native_cutoff_\(decision)", ["token": token, "reason": reason])
            return decision
        default:
            if Self.sessionOpen(token) == false {
                Self.evidence("NATIVE_CUTOFF_MIC_CLOSED", ["token": token, "reason": reason])
                return "closed"
            }
            Self.evidence("UNVERIFIED_HOLD", ["token": token, "reason": reason, "detail": "token_session_not_idle"])
            return "unverified"
        }
    }

    /// true: a live session carries [token]; false: none does; nil: controller unresponsive within queryBound.
    static func sessionOpen(_ token: Int64) -> Bool? {
        guard let mic = Self.mic else { return nil }
        let box = BoolBox()
        let done = DispatchSemaphore(value: 0)
        DispatchQueue.global().async { box.set(mic.devSessionOpen(token: token)); done.signal() }
        guard done.wait(timeout: .now() + queryBound) == .success else { return nil }
        return box.get()
    }

    /// Native -> Dart with a bounded wait; a late reply is ignored.
    private static func invokeDart(_ method: String, timeout: TimeInterval) async -> Any? {
        await withCheckedContinuation { continuation in
            let once = ResumeOnce(continuation)
            DispatchQueue.main.async {
                guard let channel = Self.channel else { once.resume(nil); return }
                channel.invokeMethod(method, arguments: nil) { reply in
                    if let error = reply as? FlutterError { once.resume("error \(error.code)") } else { once.resume(reply) }
                }
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { once.resume("timeout") }
        }
    }

    /// Append-only raw evidence, the same file the Dart side appends to.
    static func evidence(_ event: String, _ detail: [String: Any]) {
        var line: [String: Any] = ["t": ISO8601DateFormatter().string(from: Date()), "event": event, "side": "native"]
        line.merge(detail) { _, new in new }
        guard let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first,
              var data = try? JSONSerialization.data(withJSONObject: line) else { return }
        data.append(0x0A)
        let url = dir.appendingPathComponent("base_timed_capture_evidence.jsonl")
        if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
        guard let handle = try? FileHandle(forWritingTo: url) else { return }
        defer { try? handle.close() }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: data)
    }
}

// BEGIN DEV CARD POLICY
/// DEV-ONLY: what perform() does once Dart answers. Pure, so run_dev_card_policy_tests.sh compiles it alone.
enum BaseTimedCaptureCardPolicy {
    enum Action: Equatable { case keep, cutoff(String) }

    /// [outcome]: Dart's answer ("recording:<s>" or a refusal). [card]: "started" | "missing" | "wait_cancelled".
    static func afterStart(outcome: String, card: String) -> Action {
        guard outcome.hasPrefix("recording") else { return .keep }  // nothing started: nothing to stop
        switch card {
        case "started": return .keep
        case "wait_cancelled": return .cutoff("live_activity_wait_cancelled")
        default: return .cutoff("live_activity_missing")
        }
    }
}
// END DEV CARD POLICY

@available(iOS 18.0, *)
private final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Any?, Never>?
    init(_ continuation: CheckedContinuation<Any?, Never>) { self.continuation = continuation }
    func resume(_ value: Any?) {
        lock.lock(); let c = continuation; continuation = nil; lock.unlock()
        c?.resume(returning: value)
    }
}

@available(iOS 18.0, *)
private final class StringBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value: String?
    func set(_ v: String) { lock.lock(); value = v; lock.unlock() }
    func get() -> String? { lock.lock(); defer { lock.unlock() }; return value }
}

@available(iOS 18.0, *)
private final class BoolBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Bool?
    func set(_ v: Bool) { lock.lock(); value = v; lock.unlock() }
    func get() -> Bool? { lock.lock(); defer { lock.unlock() }; return value }
}
#endif
#endif
