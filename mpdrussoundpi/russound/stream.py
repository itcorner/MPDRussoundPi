# mpdrussoundpi/russound/stream.py
# This module defines the Stream class, which represents an audio stream with a URL and a name.

class Stream:
    def __init__(self, stream_url: str, stream_name: str):
        """Initialize Stream object."""
        self.stream_url = stream_url
        self.stream_name = stream_name

    def get_url(self) -> str:
        """Get the stream URL."""
        return self.stream_url
    
    def get_name(self) -> str:
        """Get the stream name."""
        return self.stream_name

