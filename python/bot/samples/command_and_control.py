import playground
import time, asyncio, os

from playground.common.io.ui.CLIShell import CLIShell, AdvancedStdio

import translations

Command = CLIShell.CommandHandler

class RemoteControlProtocol(asyncio.Protocol):
    def __init__(self, shell):
        self.transport = None
        self.shell = shell
        self.translator = translations.NetworkTranslator()
        self.buffer = b""
        self.waitingMessage = None
        self.identifier = "Unknown Object"
        self.objAttributes = []

    def connection_made(self, transport):
        self.transport = transport
        self.shell.addConnection(self)

    def data_received(self, data):
        self.buffer += data
        while True:
            if self.waitingMessage is None:
                if b"\n\n" in self.buffer:
                    index = self.buffer.index(b"\n\n")
                    message = self.buffer[:index]
                    self.buffer = self.buffer[index+2:]
                    self.waitingMessage = self.translator.processHeader(message)
                else: return
            else:
                headerType, headerArg, headers = self.waitingMessage
                contentLength = int(headers.get(b"Content_length", "0"))
                if len(self.buffer) < contentLength:
                    return
                body, self.buffer = self.buffer[:contentLength], self.buffer[contentLength:]
                self.waitingMessage = None

                try:
                    cmd = self.translator.unmarshallFromNetwork(headerType, headerArg, headers, body)
                    self.shell.handleNetworkData(self, cmd)
                except Exception as e:
                    print("Could not handle message", headerType, headerArg, headers, body)
                    self.shell.handleNetworkException(self, e)

    def connection_lost(self, reason=None):
        self.shell.removeConnection(self)

class RemoteConsole(CLIShell):
    STD_PROMPT = "[null] >> "

    DIRECTIONS_SHORT = {
        "n":"north",
        "ne":"north-east",
        "e":"east",
        "se":"south-east",
        "s":"south",
        "sw":"south-west",
        "w":"west",
        "nw":"north-west"
    }

    def __init__(self, port, serverFamily="default"):
        super().__init__(prompt=self.STD_PROMPT)
        self._protocolId = 0
        self._selected = None
        self._protocols = {}
        self.map = [['x' for i in range(100)] for i in range(100)]

        switchobjectHandler = Command("switch",
                                      "Switch object to control",
                                      self._switchObjectCommand)
        sendcommandHandler  = Command("send",
                                      "Send command to object",
                                      self._sendCommand)
        listobjectsHandler  = Command("list",
                                      "list current connections",
                                      self._listCommand)
        reprogramHandler    = Command("reprogram",
                                      "reprogram bot. Careful. Don't BRICK it.",
                                      self._reprogramCommand)
        downloadBrainHandler= Command("download_brain",
                                      "download the bot's current brain as a tar ball.",
                                      self._downloadBrainCommand)
        printmapHandler     = Command("printmap",
                                      "print current collected map.",
                                      self._printmap)
        self.registerCommand(switchobjectHandler)
        self.registerCommand(sendcommandHandler)
        self.registerCommand(listobjectsHandler)
        self.registerCommand(reprogramHandler)
        self.registerCommand(downloadBrainHandler)

        coro = playground.create_server(lambda: RemoteControlProtocol(self), port=port, family=serverFamily)
        asyncio.ensure_future(coro)

    def addConnection(self, protocol):
        self._protocolId += 1
        self._protocols[self._protocolId] = protocol

    def removeConnection(self, protocol):
        k = None
        for k in self._protocols:
            if self._protocols[k] == protocol: break
        if k is not None:
            del self._protocols[k]

    def handleNetworkException(self, protocol, e):
        self.transport.write("Network Failure: {}\n\n".format(e))

    def createObjectDisplay(self, objectData, indent=""):
       s = ""
       for key, value in objectData:
           s += "{}{}: {}\n".format(indent, key, value)
       return s 

    def createScanResultsDisplay(self, scanResults):
        mapPart = ""
        textPart = ""
        mapLine = ""
        lastY = None
        for coord, objDataList in scanResults:
            x,y = coord
            if y != lastY:
                mapPart = mapLine + "\n" + mapPart
                lastY = y
                mapLine = ""
            terrain = None
            obj = None
            for objData in objDataList:
                d = dict(objData)
                if d["type"] == "terrain":
                    terrain = d["identifier"]
                elif d["type"] == "object":
                    obj = self.createObjectDisplay(objData, indent="\t")
            if obj is not None:
                mapLine += "O"
                textPart += "Object at {}: \n{}\n".format(coord, obj)
            elif terrain == "land":
                mapLine += "#"
            elif terrain == "water":
                mapLine += "="
            if self.map[99-y][x] == 'x':
                if terrain == "land":
                    self.map[99-y][x] = '#'                   # may need to be revised for different map
                elif terrain == "water":
                    self.map[99-y][x] = '='


        mapPart = mapLine + "\n" + mapPart + "\n"
        return mapPart + textPart + "\n"
        

    def handleNetworkData(self, protocol, data):
        if isinstance(data, translations.BrainConnectResponse):
            if protocol.objAttributes != data.attributes:
                self.transport.write("Brain Connected. Attributes={}\n".format(data.attributes))
                self.transport.write("Either new connection or object change")
                protocol.translator = translations.NetworkTranslator(*data.attributes)
                protocol.identifier = data.identifier
                protocol.objAttributes = data.attributes
                self.transport.write("Attributes Loaded\n\n")
            else:
                return # Treat as heartbeat (ignore). 
        elif isinstance(data, translations.FailureResponse):
            self.transport.write("Something's wrong!: {}\n\n ".format( data.message))
        elif isinstance(data, translations.ResultResponse):
            self.transport.write("Result: {}\n\n".format(data.message))
        elif isinstance(data, translations.ScanResponse):
            self.transport.write(self.createScanResultsDisplay(data.scanResults))
            self.transport.write("\n")
        elif isinstance(data, translations.MoveCompleteEvent):
            self.transport.write("Move result: {}\n\n".format(data.message))
        elif isinstance(data, translations.ObjectMoveEvent):
            if data.status == "insert":
                verb = "arrived at"
            else:
                verb = "left"
            self.transport.write("{} {} {}\n\n".format(data.objectIdentifier, verb, data.location)) 
        elif isinstance(data, translations.StatusResponse):
            self.transport.write("{} status:\n{}\n".format(protocol.identifier, self.createObjectDisplay(data.data, indent="\t")))
        elif isinstance(data, translations.DamageEvent):
            self.transport.write("{} hit {} for {} points of damage (took {} points of damage). {}".format(protocol.identifier, data.targetObjectIdentifier, data.targetDamage, data.damage, data.message))
        # Damage by landmines. Newly added
        elif isinstance(data, translations.DamageByMinesEvent):
            self.transport.write("{} attacked by landmines. Lost {} hitpoints. {}".format(protocol.identifier, data.damage, data.message))
        # -------------------------------
        elif isinstance(data, translations.ReprogramResponse):
            self.transport.write("Reprogram of {} {}. {}\n\n".format(data.path, (data.success and "successful" or "unsuccessful"), data.message))
        elif isinstance(data, translations.DownloadBrainResponse):
            tarData = data.data
            tarName = "brain.{}.tar.gz".format(time.time())
            with open(tarName, "wb+") as f:
                f.write(tarData)
            self.transport.write("Downloaded brain as {}.\n\n".format(tarName))
        else:
            self.transport.write("Got {}\n\n".format(data))
        self.transport.refreshDisplay()

    def _listCommand(self, writer):
        objKeys = list(self._protocols.keys())
        objKeys.sort()
        for k in objKeys:
            writer("Object {} at {}\n".format(k, self._protocols[k].transport.get_extra_info("peername")))
        writer("\n")

    def _switchObjectCommand(self, writer, arg1):
        objId = int(arg1)
        if objId not in self._protocols:
            writer("No object {}\n".format(arg1))
            self.prompt = self.STD_PROMPT
        else:
            self._selected = objId
            writer("Object {} selected\n".format(arg1))
            self.prompt = "[{}] >> ".format(arg1)
        writer("\n")

    def _downloadBrainCommand(self, writer):
        protocol = self._protocols.get(self._selected, None)
        if self._selected is None:
            writer("No remote object selected.\n\n")
            return
        if protocol is None:
            writer("Selected object no longer available. \n\n")
            self.prompt = self.STD_PROMPT
            return
        cmdObj = translations.DownloadBrainCommand()
        sendData = protocol.translator.marshallToNetwork(cmdObj)
        protocol.transport.write(sendData) 
        writer("Download command sent. \n\n")



    def _reprogramCommand(self, writer, subcmd, *args):
        args = list(args) # convert to list... I want to use .pop
        if self._selected is None:
            writer("No remote object selected.\n\n")
            return

        protocol = self._protocols.get(self._selected, None)
        if protocol is None:
            writer("Selected object no longer available.\n\n")
            self.prompt = self.STD_PROMPT
            return
        if subcmd.lower() == "write":
            remotePath = args.pop(0)
            localPath = args.pop(0)
            restartNetworking = ("restart-networking" in args)
            restartBrain      = ("restart-brain"      in args)
            if not os.path.exists(localPath):
                writer("No such file {}\n\n".format(localPath))
                return
            with open(localPath, "rb") as f:
                data = f.read()
            cmdObj = translations.ReprogramCommand(remotePath, data, restartBrain, restartNetworking, deleteFile=False)
        elif subcmd.lower() == "delete":
            remotePath = args.pop(0)
            restartNetworking = ("restart-networking" in args)
            restartBrain      = ("restart-brain"      in args)
            cmdObj = translations.ReprogramCommand(remotePath, b"", restartBrain, restartNetworking, deleteFile=True)
        else:
            writer("No such reporgram command {}\n\n".format(subcmd))
            return
        choice = input("This is your last chance to cancel reprogramming {}. Y to continue.".format(remotePath))
        if choice.lower().startswith('y'):
            sendData = protocol.translator.marshallToNetwork(cmdObj)
            protocol.transport.write(sendData) 
            writer("Reprogram command send.\n\n")
        else:
            writer("Cancelled\n\n")

    def _printmap(self,writer):
        map_string = ""
        map_line = ""
        for line in self.map:
            map_line += "".join(line)
            map_line += "\n"
            map_string += map_line
            map_line = ""


        writer("Current Explored Map:\n")
        writer(map_string)
        return
        

    def _sendCommand(self, writer, cmd, *args):
        if self._selected is None:
            writer("No remote object selected.\n\n")
            return

        protocol = self._protocols.get(self._selected, None)
        if protocol is None:
            writer("Selected object no longer available.\n\n")
            self.prompt = self.STD_PROMPT
            return

        if cmd == "scan":
            cmdObj = translations.ScanCommand()
            sendData = protocol.translator.marshallToNetwork(cmdObj)
            protocol.transport.write(sendData)
            writer("Scan Message Sent.\n\n")
        elif cmd == "move":
            if len(args) != 1:
                writer("Require a direction argument (N, NE, E, SE, S, SW, W, NW)\n\n")
                return
            direction = args[0].lower()
            if direction in self.DIRECTIONS_SHORT:
                direction = self.DIRECTIONS_SHORT[direction]
            if direction not in self.DIRECTIONS_SHORT.values():
                writer("Unknown direction {}\n\n".format(direction))
                return
            cmdObj = translations.MoveCommand(direction)
            sendData = protocol.translator.marshallToNetwork(cmdObj)
            protocol.transport.write(sendData)
            writer("Move Message Sent.\n\n")
        elif cmd == "status":
            protocol.transport.write(protocol.translator.marshallToNetwork(translations.StatusCommand()))
         #---------------------------- SELF AMENDMENT ----------------------------
        elif cmd == "start" :
            if len(args) != 4:
                writer("Require start coordination and end coordination!\n\n")
                return
            start_x = args[0]
            start_y = args[1]
            end_x = args[2]
            end_y = args[3]
            cmdObj = translations.StartCommand(start_x,start_y,end_x,end_y)
            sendData = protocol.translator.marshallToNetwork(cmdObj)
            protocol.transport.write(sendData)
            writer(str(sendData))
            writer("\n\nThe bot is starting movement. \n\n")
            return

        elif cmd == "stop" :
            cmdObj = translations.StopCommand()
            sendData = protocol.translator.marshallToNetwork(cmdObj)
            protocol.transport.write(sendData)
            writer(str(sendData))
            writer("\n\nThe bot stop movement. \n\n")
            return
        elif cmd == "continue" :
            cmdObj = translations.ContinueCommand()
            sendData = protocol.translator.marshallToNetwork(cmdObj)
            protocol.transport.write(sendData)
            writer(str(sendData))
            writer("\n\nThe bot continue auto. \n\n")
        elif cmd == "auto" :
            if len(args) != 2:
                writer("Require start coordination!\n\n")
                return
            start_x = args[0]
            start_y = args[1]
            cmdObj = translations.AutoCommand(start_x, start_y)
            sendData = protocol.translator.marshallToNetwork(cmdObj)
            protocol.transport.write(sendData)
            writer("\n\nAuto mod Start.\n\n")
        #------------------------------------------------------------------------
        else:
            writer("Unknown Command {}\n\n".format(cmd))


    def stop(self):
        # use list() to make a copy... otherwise closing the protocol
        # removes it from list, changing the size during iteration
        # and causing an error
        for protocol in list(self._protocols.values()):
            try:
                protocol.transport.close()
            except:
                pass
        asyncio.get_event_loop().stop()

    def start(self):
        loop = asyncio.get_event_loop()
        self.registerExitListener(lambda reason: loop.call_later(1.0, self.stop))
        AdvancedStdio(self)


if __name__=="__main__":
    import sys

    kargs = {"--family":"default", "--port":"10013"}
    args = []
    for arg in sys.argv:
        if arg.startswith("--"):
            if "=" in arg:
                k,v = arg.split("=")
            else:
                k,v = arg, True
            kargs[k] = v
        elif arg.startswith('-'):
            kargs[arg] = True
        else:
            args.append(arg)

    shell = RemoteConsole(int(kargs["--port"]), kargs["--family"])
    asyncio.get_event_loop().call_soon(shell.start)
    asyncio.get_event_loop().run_forever() 
