import Foundation

/// A line to draw: one measured column against the swept axis (or the point number).
public struct Series: Equatable, Sendable {
    public var x: Column
    public var y: Column
    public var points: [Point]

    public init(x: Column, y: Column, points: [Point]) {
        self.x = x
        self.y = y
        self.points = points
    }
}

public enum Plot {
    static let pointNumber = Column(key: "point", unit: "", kind: "index")

    /// What to draw x against: the only swept axis, else (several axes, or none) the point number.
    public static func xColumn(_ axes: [Column]) -> Column {
        let swept = axes.filter { $0.kind != "repeat" }
        return swept.count == 1 ? swept[0] : pointNumber
    }

    /// The scalar columns one can plot.
    public static func measured(_ measures: [Column]) -> [Column] {
        measures.filter { $0.kind == "scalar" }
    }

    /// `y` against `x` from data rows, skipping rows where either is missing or not a number.
    public static func series(rows: [[String: JSONValue]], x: Column, y: Column, offset: Int = 0) -> Series {
        var points: [Point] = []
        for (n, row) in rows.enumerated() {
            let xv = x == pointNumber ? (row["i"]?.double ?? Double(offset + n)) : row[x.key]?.double
            guard let xv, let yv = row[y.key]?.double, xv.isFinite, yv.isFinite else { continue }
            points.append(Point(x: xv, y: yv))
        }
        return Series(x: x, y: y, points: points)
    }
}

/// The rows of the run in progress, fetched a few at a time (GET /api/live?since=n).
public struct LiveData: Equatable, Sendable {
    public private(set) var runID: String?
    public private(set) var axes: [Column] = []
    public private(set) var measures: [Column] = []
    public private(set) var rows: [[String: JSONValue]] = []
    public private(set) var trace: Trace?

    public init() {}

    /// Where the next fetch starts.
    public var since: Int { rows.count }

    /// Add what /api/live?since=`since` answered. True when that was the middle of a new run: fetch again from 0.
    @discardableResult
    public mutating func take(_ live: LiveRun?, since: Int) -> Bool {
        guard let live else { return false }
        if live.runID != runID {
            self = LiveData()
            runID = live.runID
            axes = live.axes
            measures = live.measures
            if since != 0 { return true }
        }
        rows += live.rows
        if let t = live.trace { trace = t }
        return false
    }

    public var measured: [Column] { Plot.measured(measures) }

    public func series(_ y: Column) -> Series {
        Plot.series(rows: rows, x: Plot.xColumn(axes), y: y)
    }
}
