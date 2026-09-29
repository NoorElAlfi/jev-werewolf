"""Thin entry point: plays one verbose game. The logic lives in ww/game/.

Needs Ollama running with the model from ww/config.py, and TYPESAFE_API_KEY in .env.
Use ww.game.run_evaluation() for batch stats.
"""

from ww.game import play_game

if __name__ == "__main__":
    play_game(verbose=True)
