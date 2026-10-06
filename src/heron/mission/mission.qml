import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Heron 1.0

Item {
    id: root
    required property var heron
    width: 1440
    height: 900
    Rectangle { anchors.fill: parent; color: "#101010" }

    Rectangle {
        id: header
        anchors { top: parent.top; left: parent.left; right: parent.right }
        height: 64
        color: "#191919"
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: "#373737" }
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 22
            anchors.rightMargin: 22
            spacing: 14
            ColumnLayout {
                spacing: 0
                Text { text: "GROUND STATION"; color: "#f2f2f2"; font.pixelSize: 13; font.bold: true; font.letterSpacing: 1.2 }
            }
            Item { Layout.fillWidth: true }
            Rectangle {
                radius: 12; color: heron.connectionLabel === "ONLINE" ? "#303030" : "#242424"
                implicitWidth: conn.implicitWidth + 22; implicitHeight: 28
                Row { id: conn; anchors.centerIn: parent; spacing: 8
                    Rectangle { width: 7; height: 7; radius: 4; anchors.verticalCenter: parent.verticalCenter; color: heron.connectionLabel === "ONLINE" ? "#eeeeee" : "#858585" }
                    Text { text: heron.connectionLabel; color: heron.connectionLabel === "ONLINE" ? "#eeeeee" : "#b5b5b5"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1 }
                }
            }
            Rectangle { width: 1; height: 26; color: "#414141" }
            Rectangle {
                radius: 12
                color: heron.armed ? "#273b2e" : "#352929"
                border.color: heron.armed ? "#75d39a" : "#d58a8a"
                implicitWidth: armedText.implicitWidth + 22; implicitHeight: 28
                Text {
                    id: armedText
                    anchors.centerIn: parent
                    text: heron.armed ? "ARMED" : "DISARMED"
                    color: heron.armed ? "#a8ebbf" : "#efaaaa"
                    font.pixelSize: 10; font.bold: true; font.letterSpacing: 0.8
                }
            }
            Rectangle { width: 1; height: 26; color: "#414141" }
            Rectangle {
                radius: 12
                color: "#242424"
                implicitWidth: batteryText.implicitWidth + 22; implicitHeight: 28
                Text {
                    id: batteryText
                    anchors.centerIn: parent
                    text: heron.batteryLabel
                    color: "#dedede"
                    font.pixelSize: 10; font.bold: true; font.letterSpacing: 0.8
                }
            }
            Rectangle { width: 1; height: 26; color: "#414141" }
            Text { text: heron.modeLabel; color: "#dedede"; font.pixelSize: 12; font.bold: true }
        }
    }

    Rectangle {
        id: rail
        anchors { top: header.bottom; bottom: footer.top; left: parent.left }
        width: 314
        color: "#151515"
        Rectangle { anchors.right: parent.right; width: 1; height: parent.height; color: "#373737" }
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12
            Text { text: "MISSION CONTROL"; color: "#a0a0a0"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.5 }
            Rectangle {
                Layout.fillWidth: true; height: 144; radius: 12; color: "#1e1e1e"; border.color: "#393939"
                Column {
                    anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; margins: 14 }
                    spacing: 4
                    Text { text: "VEHICLE TELEMETRY"; color: "#a1a1a1"; font.pixelSize: 9; font.bold: true; font.letterSpacing: 1 }
                    Text { text: heron.telemetryLabel; color: "#e5e5e5"; font.pixelSize: 12; elide: Text.ElideRight; width: parent.width }
                    Text { text: heron.missionLabel; color: "#b4b4b4"; font.pixelSize: 11; elide: Text.ElideRight; width: parent.width }
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                    spacing: 9
                    Text { text: "WAYPOINT PLAN"; color: "#a0a0a0"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.3 }
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        rowSpacing: 7
                        columnSpacing: 8
                        ActionButton { text: "↑  UPLOAD"; tone: "primary"; Layout.fillWidth: true; onClicked: heron.upload() }
                        ActionButton { text: "＋  LOAD"; Layout.fillWidth: true; onClicked: heron.load() }
                        HoldSlideButton {
                            text: "START MISSION"
                            Layout.columnSpan: 2
                            Layout.fillWidth: true
                            enabled: heron.missionReady
                            onActivated: heron.startMission()
                        }
                        ActionButton { text: "↓  SAVE"; Layout.fillWidth: true; onClicked: heron.save() }
                        ActionButton { text: "⌫  CLEAR ROUTE"; Layout.fillWidth: true; enabled: heron.clearAvailable; onClicked: heron.clear() }
                    }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#373737"; Layout.topMargin: 2; Layout.bottomMargin: 1 }
                    Text { text: "VEHICLE"; color: "#a0a0a0"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.3 }
                    GridLayout {
                        Layout.fillWidth: true
                        columns: 2
                        rowSpacing: 7
                        columnSpacing: 8
                        ActionButton { text: "▶  ARM"; tone: "arm"; Layout.fillWidth: true; onClicked: heron.arm() }
                        ActionButton { text: "■  DISARM"; tone: "disarm"; Layout.fillWidth: true; onClicked: heron.disarm() }
                        ActionButton { text: "≡  MANUAL MODE"; Layout.fillWidth: true; onClicked: heron.manual() }
                        ActionButton { text: "✕  STOP"; tone: "stop"; Layout.fillWidth: true; onClicked: heron.stop() }
                    }
                    Item { Layout.fillHeight: true }
            }
            Rectangle { Layout.fillWidth: true; height: 1; color: "#373737"; Layout.topMargin: 2; Layout.bottomMargin: 1 }
            Text { text: "ACTIVITY"; color: "#a0a0a0"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.5 }
            TextArea {
                Layout.fillWidth: true; Layout.fillHeight: true
                readOnly: true; text: heron.logText
                color: "#d0d0d0"; font.family: heron.monospaceFamily; font.pixelSize: 10
                wrapMode: TextEdit.Wrap; selectByMouse: true; persistentSelection: true
                background: Rectangle { color: "#111111"; radius: 8; border.color: "#383838" }
            }
        }
    }

    Rectangle {
        id: mapFrame
        anchors { top: header.bottom; left: rail.right; right: parent.right; bottom: footer.top }
        color: "#202020"
        MapCanvas {
            id: map
            objectName: "mapCanvas"
            anchors.fill: parent
            PinchHandler {
                acceptedDevices: PointerDevice.TouchPad
                target: null
                property real zoomStepScale: 1.0

                onActiveChanged: {
                    if (active) zoomStepScale = 1.0
                }
                onActiveScaleChanged: {
                    if (!active) return
                    if (activeScale >= zoomStepScale * 1.15) {
                        heron.zoomIn()
                        zoomStepScale = activeScale
                    } else if (activeScale <= zoomStepScale / 1.15) {
                        heron.zoomOut()
                        zoomStepScale = activeScale
                    }
                }
            }
        }
        Rectangle {
            visible: heron.modeLabel === "MANUAL"
            anchors { left: parent.left; bottom: parent.bottom; margins: 16 }
            width: 208; height: 100; radius: 10
            color: "#e0161616"; border.color: "#887b5a"
            Column {
                anchors.fill: parent; anchors.margins: 10; spacing: 5
                Text { text: "MANUAL INPUT  ·  " + heron.inputSource; color: "#ffe778"; font.pixelSize: 9; font.bold: true; font.letterSpacing: 0.6 }
                AxisMeter { label: "THROTTLE / PITCH"; value: heron.manualThrottle; width: parent.width }
                AxisMeter { label: "YAW / STEERING"; value: heron.manualYaw; accent: "#c8c8c8"; width: parent.width }
            }
        }
        Rectangle {
            anchors { left: parent.left; top: parent.top; margins: 16 }
            radius: 9; color: "#d0161616"; border.color: "#666666"
            width: mapHint.width + 24; height: 34
            Text { id: mapHint; anchors.centerIn: parent; text: "CLICK TO ADD   ·   DRAG MAP TO PAN   ·   PINCH TO ZOOM"; color: "#eeeeee"; font.pixelSize: 9; font.bold: true; font.letterSpacing: 0.7 }
        }
        Column {
            anchors { right: parent.right; top: parent.top; margins: 16 }
            spacing: 7
            MapControl { label: "+"; onClicked: heron.zoomIn() }
            MapControl { label: "−"; onClicked: heron.zoomOut() }
            MapControl { label: "⌖"; onClicked: heron.recenter() }
        }
        Rectangle {
            anchors { right: parent.right; bottom: parent.bottom; margins: 16 }
            radius: 7; color: "#d0181818"; border.color: "#666666"
            width: zoomText.implicitWidth + 22; height: 30
            Text { id: zoomText; anchors.centerIn: parent; text: map.imagerySourceLabel + "   ·   Z" + map.zoomLevel; color: "#eeeeee"; font.pixelSize: 9; font.bold: true; font.letterSpacing: 0.5 }
        }
    }

    Rectangle {
        id: footer
        anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
        height: 32; color: "#191919"
        Rectangle { anchors.top: parent.top; width: parent.width; height: 1; color: "#373737" }
        Text { anchors { left: parent.left; verticalCenter: parent.verticalCenter; leftMargin: 18 } text: heron.statusLabel; color: "#c2c2c2"; font.pixelSize: 10; elide: Text.ElideRight; width: parent.width - 36 }
    }

    component ActionButton: Button {
        id: control
        property string tone: "neutral"
        implicitHeight: 44
        contentItem: Text {
            text: control.text
            color: !control.enabled ? "#777777" : control.tone === "primary" ? "#171717" : "#ffffff"
            font.pixelSize: 10; font.bold: true; font.letterSpacing: 0.35
            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 8
            color: {
                if (!control.enabled) return "#1d1d1d"
                if (control.down) return control.tone === "arm" ? "#267342" : control.tone === "disarm" ? "#315d87" : control.tone === "stop" ? "#9e3030" : "#4a4a4a"
                if (control.tone === "primary") return "#e3e3e3"
                if (control.tone === "arm") return "#185c35"
                if (control.tone === "disarm") return "#24496f"
                if (control.tone === "stop") return "#7a2424"
                return "#242424"
            }
            border.color: {
                if (!control.enabled) return "#353535"
                if (control.tone === "primary") return "#ffffff"
                if (control.tone === "arm") return "#75d39a"
                if (control.tone === "disarm") return "#91b9e3"
                if (control.tone === "stop") return "#ff8585"
                return "#444444"
            }
        }
    }
    component HoldSlideButton: Item {
        id: holdSlide
        property string text: ""
        property real progress: 0
        signal activated()
        implicitHeight: 50

        Rectangle {
            anchors.fill: parent
            radius: 9
            color: holdSlide.enabled ? "#132b1d" : "#292929"
            border.width: 1
            border.color: holdSlide.enabled ? "#65d895" : "#454545"
        }
        Rectangle {
            x: 4; y: 4
            width: Math.max(0, (parent.width - 8) * holdSlide.progress)
            height: parent.height - 8
            radius: 8
            color: "#17643a"
        }
        Rectangle {
            id: slideHandle
            x: 5 + (holdSlide.width - height - 10) * holdSlide.progress
            y: 5
            width: holdSlide.height - 10
            height: holdSlide.height - 10
            radius: 8
            color: holdSlide.enabled ? "#d8ffe5" : "#555555"
            border.color: holdSlide.enabled ? "#77e59e" : "#666666"
            Text {
                anchors.centerIn: parent
                text: "»"
                color: "#15512e"
                font.pixelSize: 23
                font.bold: true
            }
        }
        Text {
            anchors.centerIn: parent
            text: !holdSlide.enabled ? "ADD WAYPOINTS TO START" :
                  holdSlide.progress > 0.05 ? "KEEP SLIDING TO START" : holdSlide.text + "     HOLD & SLIDE →"
            color: holdSlide.enabled ? "#f2fff5" : "#888888"
            font.pixelSize: 11
            font.bold: true
            font.letterSpacing: 0.8
        }
        MouseArea {
            anchors.fill: parent
            enabled: holdSlide.enabled
            preventStealing: true
            hoverEnabled: true
            property bool tracking: false
            function updateProgress(mouseX) {
                const startX = slideHandle.width / 2 + 5
                const endX = holdSlide.width - slideHandle.width / 2 - 5
                holdSlide.progress = Math.max(0, Math.min(1, (mouseX - startX) / (endX - startX)))
            }
            onPressed: function(mouse) {
                if (mouse.x > slideHandle.width + 10) return
                tracking = true
                updateProgress(mouse.x)
            }
            onPositionChanged: function(mouse) {
                if (tracking) updateProgress(mouse.x)
            }
            onReleased: function(mouse) {
                if (!tracking) return
                updateProgress(mouse.x)
                const completed = holdSlide.progress >= 0.9
                tracking = false
                holdSlide.progress = 0
                if (completed) holdSlide.activated()
            }
            onCanceled: {
                tracking = false
                holdSlide.progress = 0
            }
        }
    }
    component MapControl: Button {
        property string label: ""
        implicitWidth: 38; implicitHeight: 38
        contentItem: Text { text: parent.label; color: "#f0f0f0"; font.pixelSize: 17; font.bold: true; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
        background: Rectangle { radius: 9; color: parent.down ? "#484848" : "#d01b1b1b"; border.color: "#6b6b6b" }
    }
    component AxisMeter: Item {
        property string label: ""
        property int value: 0
        property color accent: "#ffd84a"
        implicitHeight: 25
        Column {
            anchors.fill: parent
            spacing: 3
            RowLayout {
                width: parent.width
                Text { text: label; color: "#a8a8a8"; font.pixelSize: 8; font.bold: true; font.letterSpacing: 0.7 }
                Item { Layout.fillWidth: true; height: 1 }
                Text { text: value > 0 ? "+" + value : "" + value; color: "#eeeeee"; font.family: heron.monospaceFamily; font.pixelSize: 9; font.bold: true }
            }
            Rectangle {
                id: meterTrack
                width: parent.width; height: 6; radius: 3; color: "#363636"
                Rectangle {
                    height: parent.height - 2; radius: 2; y: 1
                    x: value < 0 ? parent.width / 2 - parent.width / 2 * Math.min(1, Math.abs(value) / 1000) : parent.width / 2
                    width: parent.width / 2 * Math.min(1, Math.abs(value) / 1000)
                    color: accent
                }
                Rectangle { x: parent.width / 2 - 1; y: -1; width: 2; height: 8; radius: 1; color: "#f0f0f0" }
            }
        }
    }
}
