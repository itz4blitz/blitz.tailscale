import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "blitz.tailscale"
  ipcTarget: "blitz.tailscale"

  property bool ready: false
  property string status: "unavailable"
  property int daemonOnline: 0
  property int daemonTotal: 0
  property int onlineCount: 0
  property int totalCount: 0
  property var daemons: []
  property string selectedDaemonId: ""
  property string selectedName: ""
  property string filterText: ""
  property string logText: ""

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dimColor: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.55)
  readonly property color trackColor: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.22)
  readonly property color okColor: Qt.rgba(0.45, 0.82, 0.52, 1)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string collectorPath: {
    var url = String(Qt.resolvedUrl("tailscale_collect.py"))
    return url.startsWith("file://") ? url.substring(7) : url
  }

  readonly property bool overview: selectedDaemonId === ""

  readonly property var selectedDaemon: {
    if (!selectedDaemonId) return null
    for (var i = 0; i < daemons.length; i++)
      if (daemons[i].id === selectedDaemonId) return daemons[i]
    return null
  }

  readonly property var visibleItems: {
    var q = String(filterText || "").toLowerCase()
    var list = []
    var items = selectedDaemon && selectedDaemon.items ? selectedDaemon.items : []
    for (var i = 0; i < items.length; i++) {
      var item = items[i]
      if (!q || String(item.name || "").toLowerCase().indexOf(q) >= 0)
        list.push(item)
    }
    return list
  }

  readonly property var serviceItems: {
    var list = []
    for (var i = 0; i < visibleItems.length; i++)
      if (visibleItems[i].kind === "service") list.push(visibleItems[i])
    return list
  }

  readonly property var machineItems: {
    var list = []
    for (var i = 0; i < visibleItems.length; i++)
      if (visibleItems[i].kind !== "service") list.push(visibleItems[i])
    return list
  }

  readonly property var selectedItem: {
    if (!selectedName) return null
    for (var i = 0; i < visibleItems.length; i++)
      if (visibleItems[i].name === selectedName) return visibleItems[i]
    return null
  }

  function apply(payload) {
    try { var d = JSON.parse(String(payload)) } catch (e) { return }
    ready = d.ready === true
    status = String(d.status || (ready ? "ok" : "unavailable"))
    daemonOnline = Number(d.daemonOnline || 0)
    daemonTotal = Number(d.daemonTotal || 0)
    onlineCount = Number(d.onlineCount || 0)
    totalCount = Number(d.totalCount || 0)
    daemons = Array.isArray(d.daemons) ? d.daemons : []
  }

  function refresh() {
    if (!collectProc.running) collectProc.running = true
  }

  function selectDaemon(id) {
    selectedDaemonId = id
    selectedName = ""
    logText = ""
  }

  function backToOverview() {
    selectedDaemonId = ""
    selectedName = ""
    filterText = ""
    logText = ""
  }

  function selectItem(name) {
    selectedName = name
  }

  function httpText(item) {
    if (!item || item.http === undefined || item.http === null || item.http === "")
      return ""
    return "HTTP " + item.http
  }

  function lastSeenText(item) {
    if (!item || item.online) return item && item.self ? "this device" : ""
    var raw = String(item.lastSeen || "")
    if (!raw) return "offline"
    return raw.replace("T", " ").replace("Z", " UTC")
  }

  function loadLogs() {
    if (!selectedDaemonId) { logText = ""; return }
    logProc.command = ["python3", root.collectorPath, "logs", selectedDaemonId]
    logProc.running = true
  }

  function openItem(item) {
    if (!root.bar || !item || !item.url) return
    var quoted = typeof root.bar.shellQuote === "function" ? root.bar.shellQuote(item.url) : item.url
    root.bar.run("xdg-open " + quoted)
  }

  function pingItem(item) {
    if (!item || !selectedDaemonId || pingProc.running) return
    logText = "Pinging " + item.name + "…"
    pingProc.command = ["python3", root.collectorPath, "action", selectedDaemonId, "ping", item.name]
    pingProc.running = true
  }

  function openLogs() {
    if (!root.bar || !selectedDaemon) return
    var unit = selectedDaemon.unit || "tailscaled"
    root.bar.run("omarchy-launch-or-focus-tui journalctl -fu " + unit)
  }

  function openConsole(daemonId) {
    if (root.bar) root.bar.run("xdg-open https://login.tailscale.com/admin")
  }

  function triggerPress(button) {
    if (button === Qt.RightButton) { root.openConsole(); return }
    root.toggle()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Process {
    id: collectProc
    command: ["python3", root.collectorPath]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.apply(text) }
  }

  Process {
    id: pingProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.logText = String(text || "no ping output")
    }
  }

  onOpenedChanged: if (!root.opened) { root.backToOverview() }

  Process {
    id: logProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.logText = String(text || "")
    }
  }

  Timer {
    interval: 5000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    pressable: true
    horizontalMargin: 8
    fixedWidth: tsChip.implicitWidth + scaledHorizontalMargin * 2
    onPressed: function(b) { root.triggerPress(b) }

    Row {
      id: tsChip
      anchors.centerIn: parent
      spacing: Style.space(8)

      Text {
        text: "ts"
        color: root.ready ? root.dimColor : (root.bar ? root.bar.urgent : Color.urgent)
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        anchors.verticalCenter: parent.verticalCenter
      }

      Repeater {
        model: root.daemons
        delegate: Text {
          required property var modelData
          text: String(modelData.label || modelData.id || "?").charAt(0).toUpperCase()
          color: modelData.online ? root.okColor : root.trackColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      Text {
        visible: root.ready
        text: root.onlineCount + "/" + root.totalCount
        color: root.onlineCount > 0 ? root.okColor : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
        anchors.verticalCenter: parent.verticalCenter
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    contentWidth: panel.fittedContentWidth(Style.space(560))
    contentHeight: panel.fittedContentHeight(Style.space(620), Style.space(720))

    Item {
      anchors.fill: parent

      Column {
        id: panelColumn
        anchors.fill: parent
        spacing: Style.space(12)

        PanelHero {
          width: parent.width
          title: root.overview
            ? (root.ready ? "Tailscale" : "Tailscale unavailable")
            : (root.selectedDaemon ? root.selectedDaemon.label : "Tailscale")
          meta: root.ready
            ? (root.overview
              ? (root.daemonOnline + "/" + root.daemonTotal + " tailnets online · " + root.onlineCount + " up")
              : ((root.selectedDaemon ? root.selectedDaemon.onlineCount + "/" + root.selectedDaemon.totalCount + " up" : "")
                + (root.selectedDaemon && root.selectedDaemon.suffix ? " · " + root.selectedDaemon.suffix : "")))
            : root.status
          foreground: root.foreground
          fontFamily: root.fontFamily
          trailingControl: Component {
            Row {
              spacing: Style.space(6)
              Button {
                visible: !root.overview
                text: "Back"
                foreground: root.foreground
                onClicked: root.backToOverview()
              }
              Button {
                text: "console"
                foreground: root.foreground
                onClicked: root.openConsole()
              }
            }
          }
        }

        Item {
          width: parent.width
          height: parent.height - y
          visible: root.overview

          Column {
            anchors.fill: parent
            spacing: Style.space(10)

            Repeater {
              model: root.daemons
              delegate: BorderSurface {
                required property var modelData
                width: parent.width
                height: Style.space(88)
                radius: Style.spacing.labelGap
                color: "transparent"
                borderSpec: Border.controlSpec("normal", root.foreground, Color.accent)

                Row {
                  anchors.fill: parent
                  anchors.margins: Style.space(16)
                  spacing: Style.space(14)

                  Rectangle {
                    width: Style.space(12)
                    height: width
                    radius: width / 2
                    color: modelData.online ? root.okColor : root.trackColor
                    anchors.verticalCenter: parent.verticalCenter
                  }

                  Column {
                    width: parent.width - Style.space(40)
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Style.space(4)
                    Text {
                      text: modelData.label
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      font.bold: true
                    }
                    Text {
                      text: (modelData.online ? "online" : "offline")
                        + " · " + modelData.onlineCount + "/" + modelData.totalCount + " up"
                        + (modelData.suffix ? " · " + modelData.suffix : "")
                      color: root.dimColor
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                    Text {
                      text: "Open tailnet"
                      color: Color.accent
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }
                }

                Button {
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(16)
                  anchors.verticalCenter: parent.verticalCenter
                  text: "Admin"
                  foreground: root.foreground
                  onClicked: root.openConsole(modelData.id)
                }

                MouseArea {
                  anchors.fill: parent
                  anchors.rightMargin: Style.space(90)
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.selectDaemon(modelData.id)
                }
              }
            }

            Item { width: 1; height: Style.space(8) }

            BorderSurface {
              width: parent.width
              height: Style.space(72)
              radius: Style.spacing.labelGap
              color: Qt.rgba(0, 0, 0, 0.28)
              borderSpec: Border.none()

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.margins: Style.space(16)
                spacing: Style.space(4)
                Text {
                  text: "Daemon logs"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }
                Text {
                  text: "Open logs in terminal  ·  journalctl for this tailscaled"
                  color: root.dimColor
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                  if (root.bar)
                    root.bar.run("omarchy-launch-or-focus-tui journalctl -fu tailscaled")
                }
              }
            }
          }
        }

        Item {
          width: parent.width
          height: parent.height - y
          visible: !root.overview

          TextField {
            id: filterField
            anchors.top: parent.top
            width: parent.width
            placeholderText: "Filter services or machines"
            text: root.filterText
            onTextChanged: root.filterText = text
          }

          Row {
            id: detailRow
            anchors.top: filterField.bottom
            anchors.topMargin: Style.space(10)
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: logStrip.top
            anchors.bottomMargin: Style.space(10)
            spacing: Style.space(12)

            Flickable {
              id: daemonList
              width: Style.space(190)
              height: parent.height
              clip: true
              contentWidth: width
              contentHeight: daemonCol.implicitHeight
              boundsBehavior: Flickable.StopAtBounds

              Column {
                id: daemonCol
                width: daemonList.width
                spacing: Style.space(4)

                PanelSectionHeader {
                  text: "SERVICES"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                }

                Repeater {
                  model: root.serviceItems
                  delegate: itemRow
                }

                PanelSectionHeader {
                  text: "MACHINES"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                }

                Repeater {
                  model: root.machineItems
                  delegate: itemRow
                }
              }
            }

            Column {
              width: parent.width - Style.space(190) - parent.spacing
              height: parent.height
              spacing: Style.space(8)

              Text {
                width: parent.width
                text: root.selectedItem ? root.selectedItem.name : "Pick a service or machine"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }

              Text {
                width: parent.width
                visible: !!root.selectedItem
                text: root.selectedItem
                  ? ((root.selectedItem.self ? "this device · " : "")
                    + (root.selectedItem.kind || "") + " · "
                    + (root.selectedItem.online ? "online" : "offline")
                    + (root.httpText(root.selectedItem) ? " · " + root.httpText(root.selectedItem) : "")
                    + (root.selectedItem.ip ? " · " + root.selectedItem.ip : "")
                    + (root.lastSeenText(root.selectedItem) && !root.selectedItem.online ? " · " + root.lastSeenText(root.selectedItem) : ""))
                  : "Nothing selected — use the list, or open daemon logs below."
                color: root.dimColor
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.Wrap
              }

              Row {
                spacing: Style.space(6)
                visible: !!root.selectedItem
                Button {
                  text: "Open"
                  visible: !!(root.selectedItem && root.selectedItem.url && !root.selectedItem.self)
                  foreground: root.foreground
                  onClicked: if (root.selectedItem) root.openItem(root.selectedItem)
                }
                Button {
                  text: "Ping"
                  foreground: root.foreground
                  onClicked: if (root.selectedItem) root.pingItem(root.selectedItem)
                }
              }

              Text {
                width: parent.width
                visible: root.logText !== ""
                text: root.logText
                color: root.okColor
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.Wrap
              }
            }
          }

          BorderSurface {
            id: logStrip
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: Style.space(56)
            radius: Style.spacing.labelGap
            color: Qt.rgba(0, 0, 0, 0.28)
            borderSpec: Border.none()

            Column {
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.margins: Style.space(14)
              spacing: Style.space(2)
              Text {
                text: "Open logs in terminal"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }
              Text {
                text: "journalctl -fu " + (root.selectedDaemon && root.selectedDaemon.unit ? root.selectedDaemon.unit : "tailscaled")
                color: root.dimColor
                font.family: "monospace"
                font.pixelSize: Style.font.caption
              }
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: root.openLogs()
            }
          }
        }
      }
    }
  }

  Component {
    id: itemRow
    Rectangle {
      required property var modelData
      width: daemonCol.width
      height: Style.space(26)
      color: root.selectedName === modelData.name
        ? Style.selectedFillFor(root.foreground, Color.accent)
        : "transparent"
      radius: Style.spacing.labelGap

      Row {
        anchors.fill: parent
        anchors.leftMargin: Style.space(8)
        anchors.rightMargin: Style.space(8)
        spacing: Style.space(6)
        Rectangle {
          width: Style.space(6)
          height: width
          radius: width / 2
          color: modelData.online
            ? (modelData.http && Number(modelData.http) >= 400 ? root.bar.urgent : root.okColor)
            : root.trackColor
          anchors.verticalCenter: parent.verticalCenter
        }
        Text {
          width: parent.width - Style.space(14)
          text: modelData.name + (modelData.self ? " · this device" : "")
            + (root.httpText(modelData) ? " · " + root.httpText(modelData) : "")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          elide: Text.ElideRight
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: root.selectItem(modelData.name)
        onDoubleClicked: {
          root.selectItem(modelData.name)
          if (!modelData.self) root.openItem(modelData)
        }
      }
    }
  }
}
