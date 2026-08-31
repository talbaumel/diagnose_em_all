# ruff: noqa: I001

from src.realtime_conversation import PatientType, Test, strat_conversation


if __name__ == "__main__":
    strat_conversation(
        system_prompts="You are a kid with a common cold. You are talking to a doctor. Do not say you have a common cold, but describe your symptoms. You are a kid, so you are not very articulate. You are also a bit scared of the doctor.",
        secret_phrase="You have a common cold",
        patient_type=PatientType.COMMON_COLD_KID,
        tests=[Test("temperature", "data/sprites/thermometer.png")],
    )
