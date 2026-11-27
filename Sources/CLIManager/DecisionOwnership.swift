import Foundation

struct DecisionInspection: Decodable, Sendable {
    let schema: Int
    let entries: [Entry]

    struct Entry: Decodable, Identifiable, Sendable {
        let id: String
        let route: String
        let questions: Int
        let permissions: Int
    }
}

/// An app-owned terminal adapter can hold these tickets in memory after the
/// user opens a terminal. Terminal bytes and delegated tasks have no API here.
struct DirectTerminalDecisionRouter {
    struct Ticket: Hashable, Sendable {
        fileprivate let id = UUID()
        fileprivate init() {}
    }

    struct Request: Hashable, Sendable {
        enum Kind: Hashable, Sendable { case permission, question }
        let id: String
        let kind: Kind
    }

    struct Notice: Equatable, Sendable {
        let target: Ticket
        let request: Request
    }

    private var open: [Ticket: Set<String>] = [:]
    private var focused: Ticket?

    mutating func userOpenedTerminal() -> Ticket {
        let ticket = Ticket()
        open[ticket] = []
        return ticket
    }

    mutating func focus(_ ticket: Ticket?) {
        focused = ticket.flatMap { open[$0] == nil ? nil : $0 }
    }

    mutating func close(_ ticket: Ticket) {
        open.removeValue(forKey: ticket)
        if focused == ticket { focused = nil }
    }

    mutating func notice(for request: Request, from ticket: Ticket) -> Notice? {
        guard focused == ticket, !request.id.isEmpty, open[ticket]?.insert(request.id).inserted == true else {
            return nil
        }
        return Notice(target: ticket, request: request)
    }
}
