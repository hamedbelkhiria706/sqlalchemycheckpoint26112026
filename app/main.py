from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import create_access_token, get_current_user, hash_password, verify_password
from app.cache import generate_assistant_reply
from app.database import Base, engine, get_db
from app.models import Conversation, Message, User
from app.schemas import (
    ConversationCreate,
    ConversationDetailResponse,
    ConversationResponse,
    LoginRequest,
    MessageCreate,
    MessageTurnResponse,
    TokenResponse,
    UserCreate,
    UserResponse,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Secure Conversations API", lifespan=lifespan)


def _get_owned_conversation(
    db: Session,
    conversation_id: int,
    current_user: User,
    load_messages: bool = False,
) -> Conversation:
    stmt = select(Conversation).where(Conversation.id == conversation_id)
    if load_messages:
        stmt = stmt.options(selectinload(Conversation.messages))
    conversation = db.scalar(stmt)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    if conversation.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this conversation",
        )
    return conversation


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    normalized_email = payload.email.strip().lower()
    existing = db.scalar(select(User).where(User.email == normalized_email))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(email=normalized_email, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    normalized_email = payload.email.strip().lower()
    user = db.scalar(select(User).where(User.email == normalized_email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user_id=user.id, email=user.email)
    return TokenResponse(access_token=token)


@app.get("/auth/me", response_model=UserResponse)
def read_me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@app.post("/conversations", response_model=ConversationDetailResponse, status_code=status.HTTP_201_CREATED)
def start_conversation(
    payload: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Conversation:
    conversation = Conversation(user_id=current_user.id, title=payload.title.strip())
    db.add(conversation)
    db.flush()

    if payload.initial_message:
        user_msg = Message(
            conversation_id=conversation.id,
            role="user",
            content=payload.initial_message.strip(),
        )
        reply_text, _ = generate_assistant_reply(payload.initial_message)
        assistant_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=reply_text,
        )
        db.add_all([user_msg, assistant_msg])

    db.commit()
    return _get_owned_conversation(db, conversation.id, current_user, load_messages=True)


@app.get("/conversations", response_model=list[ConversationResponse])
def list_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Conversation]:
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == current_user.id)
        .order_by(Conversation.id.asc())
    )
    return list(db.scalars(stmt).all())


@app.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Conversation:
    return _get_owned_conversation(db, conversation_id, current_user, load_messages=True)


@app.post(
    "/conversations/{conversation_id}/messages",
    response_model=MessageTurnResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_message(
    conversation_id: int,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MessageTurnResponse:
    conversation = _get_owned_conversation(db, conversation_id, current_user)

    user_msg = Message(
        conversation_id=conversation.id,
        role="user",
        content=payload.content.strip(),
    )
    reply_text, cached = generate_assistant_reply(payload.content)
    assistant_msg = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=reply_text,
    )
    db.add_all([user_msg, assistant_msg])
    db.commit()
    db.refresh(user_msg)
    db.refresh(assistant_msg)

    return MessageTurnResponse(
        user_message=user_msg,
        assistant_message=assistant_msg,
        cached_ai_result=cached,
    )
