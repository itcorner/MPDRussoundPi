import mpd
import stream

class mpdinstance:
    def __init__(self, name: str, bind_addr: str, port: int, stream_obj: stream.Stream) -> None:
        self.name = name
        self.bind_addr = bind_addr
        self.port = port
        self.stream = stream_obj


    def play(self):
        """Connect to MPD instance and play the stream."""
        try:
            client = mpd.MPDClient()
            client.connect('localhost', self.port)  # pyright: ignore[reportUnknownMemberType]
            playlist_url = self.stream.get_url()
            if playlist_url:
                client.add(playlist_url) # pyright: ignore[reportUnknownMemberType]
                client.play() # pyright: ignore[reportUnknownMemberType]
                print(f"Now playing stream: {self.stream.get_name()}")
            client.close() # pyright: ignore[reportUnknownMemberType]
        except (mpd.ConnectionError, OSError) as e:
            print(f"Error: Could not connect to MPD for stream '{self.stream.get_name()}' on port {self.port}: {e}")

    def stop(self):
        """Stop playback of the stream in MPD."""
        try:
            client = mpd.MPDClient()
            client.connect('localhost', self.port) # pyright: ignore[reportUnknownMemberType]
            client.stop() # pyright: ignore[reportUnknownMemberType]
            client.close() # pyright: ignore[reportUnknownMemberType]
            print(f"Stopped stream: {self.stream.get_name()}")
        except (mpd.ConnectionError, OSError) as e:
            print(f"Error: Could not connect to MPD for stream '{self.stream.get_name()}' on port {self.port}: {e}")
            #
    def get_stream_status(self):
        """Get the current status of the stream from MPD."""
        try:
            client = mpd.MPDClient()
            client.connect('localhost', self.port) # pyright: ignore[reportUnknownMemberType]
            status = client.status() # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            current_song = client.currentsong() # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            client.close() # pyright: ignore[reportUnknownMemberType]
            print(f"Stream: {self.stream.get_name()}, Status: {status.get('state')}, Current Song: {current_song.get('title', 'N/A')} \n")
            return status.get('state')
        except (mpd.ConnectionError, OSError) as e:
            print(f"Error: Could not connect to MPD for stream '{self.stream.get_name()}' on port {self.port}: {e}")


