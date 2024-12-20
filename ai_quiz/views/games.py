import logging

from adrf.views import APIView as AsyncAPIView
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from ai_quiz.ai import generate_questions, generate_subtopics
from ai_quiz.models import Game, Question, Room, Topic
from ai_quiz.serializers import (
    CreateGameRequestSerializer,
    CreateGameResponseSerializer,
    StartGameRequestSerializer,
    StartGameResponseSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()


class CreateGameView(AsyncAPIView):
    permission_classes = [IsAuthenticated]

    async def validate_room(self, room_code):
        """Validate the room code and return the room object."""
        try:
            room = await Room.objects.aget(room_code=room_code, status="active")
        except Room.DoesNotExist:
            return None
        return room

    async def validate_game(self, room: Room):
        """Validate if a game is already in progress."""
        try:
            game = Game.objects.aget(room=room, status="in_progress")
            return game
        except Game.DoesNotExist:
            return None

    async def create_game(self, room, topic, n, difficulty):
        """Create a new game object associated with the room."""
        game = await Game.objects.acreate(room=room, status="waiting")
        subtopics = await generate_subtopics(topic)
        await self._fetch_and_create_questions(
            game=game,
            topic=topic,
            subtopics=subtopics.subtopics,
            n=n,
            difficulty=difficulty,
        )
        return game.id

    async def _get_or_create_topic(self, topic, subtopics):
        """Get or create a topic object with the given subtopics."""
        topic, _ = await Topic.objects.aget_or_create(name=topic)
        topic.subtopics = list(set(subtopics)) + list(set(topic.subtopics))
        await topic.asave()
        return topic

    async def _fetch_and_create_questions(
        self,
        game: Game,
        topic: str,
        subtopics: list[str],
        n: int,
        difficulty: str,
    ):
        """Fetch questions from the AI backend and create question objects."""
        questions = await generate_questions(
            topic=topic,
            subtopics=subtopics,
            n=n,
            difficulty=difficulty,
        )
        questions = await Question.objects.abulk_create(
            [
                Question(
                    game=game,
                    subtopic=question.subtopic,
                    question=question.question,
                    correct_answer=question.answer,
                    options=question.options,
                    topic=await self._get_or_create_topic(topic, subtopics),
                )
                for question in questions.questions
            ]
        )
        return questions

    @database_sync_to_async
    def validate_user(self, room, request):
        return request.user == room.host

    @swagger_auto_schema(
        request_body=CreateGameRequestSerializer,
        responses={
            201: CreateGameResponseSerializer,
        },
        operation_description="Create a room with a custom serializer",
    )
    async def post(self, request, *args, **kwargs):
        room_code = request.data.get("roomCode")
        topic = request.data.get("topic")
        subtopics = request.data.get("subtopics")
        n = request.data.get("n", 5)
        difficulty = request.data.get("difficulty", "easy")
        if not topic or not subtopics:
            return Response(
                {
                    "error": "`topic` and `subtopics` are required; `n` and `difficulty` are optional."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not room_code:
            return Response(
                {"error": "roomCode is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if the room exists
        room = await self.validate_room(room_code)
        if room is None:
            return Response(
                {"error": f"Room with code {room_code} not found or has been closed."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Check if the requesting user is the host
        if not await self.validate_user(room, request):
            return Response(
                {"error": "Only the host can create the game."},
                status=status.HTTP_403_FORBIDDEN,
            )
        game = await self.validate_game(room)

        # There is already a game in progress
        if game is not None:
            return Response(
                {
                    "gameId": game.id,
                },
                status=status.HTTP_200_OK,
            )

        game_id = await self.create_game(room, topic, n, difficulty)
        response_data = {
            "gameId": game_id,
        }
        return Response(response_data, status=status.HTTP_201_CREATED)


class StartGameView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        request_body=StartGameRequestSerializer,
        responses={
            201: StartGameResponseSerializer,
        },
        operation_description="Create a room with a custom serializer",
    )
    def post(self, request, *args, **kwargs):
        """Start the game."""
        room_code = request.data.get("roomCode")

        if not room_code:
            return Response(
                {"error": "roomCode is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if the room exists
        try:
            room = Room.objects.get(room_code=room_code)
        except Room.DoesNotExist:
            return Response(
                {"error": "Room not found."}, status=status.HTTP_404_NOT_FOUND
            )

        # Check if the requesting user is the host
        if room.host != request.user:
            return Response(
                {"error": "Only the host can start the game."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            # We need to check if all players are ready before starting the game
            if room.participants.exclude(status="ready").exists():
                return Response(
                    {"error": "All participants must be ready to start the game."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            game = room.get_waiting_game()
            game.create_leaderboard()
            game.status = "in_progress"
            game.save()
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        response_data = {
            "gameId": game.id,
        }
        return Response(response_data, status=status.HTTP_200_OK)


class EndGameView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        """End the game."""
        room_code = request.data.get("roomCode")
        game_id = request.data.get("gameId")
        if not room_code or not game_id:
            return Response(
                {"error": "roomCode and gameId required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if the room exists
        try:
            room = Room.objects.get(room_code=room_code)
        except Room.DoesNotExist:
            return Response(
                {"error": "Room not found."}, status=status.HTTP_404_NOT_FOUND
            )

        # Check if the requesting user is the host
        if room.host != request.user:
            return Response(
                {"error": "Only the host can end the game."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # End the game (e.g., setting a game state, etc.)
        try:
            room.end_game()
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        # Get the game instances that are in progress and end them

        return Response({"status": "game_ended"}, status=status.HTTP_200_OK)
