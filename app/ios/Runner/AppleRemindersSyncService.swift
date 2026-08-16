import Foundation
#if canImport(EventKit)
import EventKit
#endif

struct AppleReminderSyncRequest {
    let title: String
    let dueDate: Date?
    let notes: String
    let operationMarker: String
}

protocol AppleRemindersSyncSink {
    func reminderExists(
        operationMarker: String,
        completion: @escaping (Result<Bool, Error>) -> Void
    )
    func createReminder(_ request: AppleReminderSyncRequest) throws
}

/// Production boundary used by AppDelegate for push-driven EventKit writes.
final class AppleRemindersSyncService {
    private let idempotency: AppleRemindersSyncIdempotency
    private let sink: AppleRemindersSyncSink

    init(
        idempotency: AppleRemindersSyncIdempotency = .shared,
        sink: AppleRemindersSyncSink
    ) {
        self.idempotency = idempotency
        self.sink = sink
    }

    func sync(
        idempotencyKey: String,
        title: String,
        dueDate: Date?,
        canReconcile: Bool,
        completion: @escaping (AppleRemindersSyncOutcome) -> Void
    ) {
        idempotency.performOnce(
            idempotencyKey: idempotencyKey,
            canReconcile: canReconcile,
            reconcile: { [sink] operationMarker, callback in
                sink.reminderExists(operationMarker: operationMarker, completion: callback)
            },
            operation: { [sink] operationMarker in
                try sink.createReminder(
                    AppleReminderSyncRequest(
                        title: title,
                        dueDate: dueDate,
                        notes: "From Omi",
                        operationMarker: operationMarker
                    )
                )
            },
            completion: completion
        )
    }
}

#if canImport(EventKit)
private enum EventKitAppleRemindersSyncError: Error {
    case noDefaultCalendar
    case invalidOperationMarker
    case reminderQueryUnavailable
}

final class EventKitAppleRemindersSyncSink: AppleRemindersSyncSink {
    private let eventStore: EKEventStore

    init(eventStore: EKEventStore) {
        self.eventStore = eventStore
    }

    func reminderExists(
        operationMarker: String,
        completion: @escaping (Result<Bool, Error>) -> Void
    ) {
        let predicate = eventStore.predicateForReminders(in: nil)
        eventStore.fetchReminders(matching: predicate) { reminders in
            guard let reminders else {
                completion(.failure(EventKitAppleRemindersSyncError.reminderQueryUnavailable))
                return
            }
            completion(
                .success(
                    reminders.contains {
                        $0.url?.absoluteString == operationMarker
                    }
                )
            )
        }
    }

    func createReminder(_ request: AppleReminderSyncRequest) throws {
        guard let calendar = eventStore.defaultCalendarForNewReminders() else {
            throw EventKitAppleRemindersSyncError.noDefaultCalendar
        }
        let reminder = EKReminder(eventStore: eventStore)
        reminder.title = request.title
        reminder.notes = request.notes
        guard let operationMarkerURL = URL(string: request.operationMarker) else {
            throw EventKitAppleRemindersSyncError.invalidOperationMarker
        }
        reminder.url = operationMarkerURL
        reminder.calendar = calendar
        if let dueDate = request.dueDate {
            reminder.dueDateComponents = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute], from: dueDate
            )
        }
        try eventStore.save(reminder, commit: true)
    }
}
#endif
