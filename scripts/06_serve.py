#!/usr/bin/env python3
"""
Script d'inférence pour servir le modèle entraîné.
Supporte à la fois le mode interactif CLI et le mode API serveur.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import AutoTokenizer

from utils.model_utils import load_pretrained_model


class GenerationRequest(BaseModel):
    """Modèle de requête pour l'API de génération."""
    prompt: str
    max_new_tokens: Optional[int] = 100
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9
    repetition_penalty: Optional[float] = 1.1
    do_sample: Optional[bool] = True
    template: Optional[str] = "chatml"


class GenerationResponse(BaseModel):
    """Modèle de réponse pour l'API de génération."""
    response: str
    prompt: str
    generation_config: Dict


class ModelServer:
    """Serveur de modèle avec génération de texte."""
    
    def __init__(self, model_path: str, tokenizer_path: Optional[str] = None):
        """
        Initialise le serveur avec le modèle et tokenizer.
        
        Args:
            model_path: Chemin vers le modèle entraîné
            tokenizer_path: Chemin vers le tokenizer (optionnel)
        """
        print(f"Chargement du modèle depuis {model_path}...")
        self.model = load_pretrained_model(model_path)
        self.model.eval()
        
        # Chargement du tokenizer
        if tokenizer_path:
            self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
        else:
            self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        
        # Optimisations pour l'inférence
        self.device = next(self.model.parameters()).device
        
        print(f"Modèle chargé sur {self.device}")
        print(f"Paramètres: {sum(p.numel() for p in self.model.parameters()):,}")
    
    def format_prompt(self, prompt: str, template: str = "chatml") -> str:
        """
        Formate le prompt selon le template spécifié.
        
        Args:
            prompt: Prompt utilisateur
            template: Template à utiliser
            
        Returns:
            Prompt formaté
        """
        if template == "chatml":
            return f"<|im_start|>user\n{prompt}\n<|im_end|>\n<|im_start|>assistant\n"
        elif template == "chat":
            return f"Human: {prompt}\n\nAssistant: "
        elif template == "instruct":
            return f"### Instruction:\n{prompt}\n\n### Response:\n"
        else:
            # Template raw - pas de formatage
            return prompt
    
    def generate(
        self,
        prompt: str,
        max_new_tokens: int = 100,
        temperature: float = 0.7,
        top_p: float = 0.9,
        repetition_penalty: float = 1.1,
        do_sample: bool = True,
        template: str = "chatml"
    ) -> tuple[str, str]:
        """
        Génère une réponse à partir du prompt.
        
        Args:
            prompt: Prompt utilisateur
            max_new_tokens: Nombre maximum de nouveaux tokens
            temperature: Température de génération
            top_p: Top-p sampling
            repetition_penalty: Pénalité de répétition
            do_sample: Utiliser l'échantillonnage
            template: Template de formatage
            
        Returns:
            Tuple (prompt_formaté, réponse_générée)
        """
        # Formatage du prompt
        formatted_prompt = self.format_prompt(prompt, template)
        
        # Tokenisation
        inputs = self.tokenizer(
            formatted_prompt,
            return_tensors="pt",
            truncation=True,
            max_length=2048
        ).to(self.device)
        
        # Configuration de génération
        generation_config = {
            "max_new_tokens": max_new_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "repetition_penalty": repetition_penalty,
            "do_sample": do_sample,
            "pad_token_id": self.tokenizer.eos_token_id,
            "eos_token_id": self.tokenizer.eos_token_id,
            "early_stopping": True,
            "no_repeat_ngram_size": 3
        }
        
        # Génération
        with torch.no_grad():
            outputs = self.model.generate(
                inputs.input_ids,
                attention_mask=inputs.attention_mask,
                **generation_config
            )
        
        # Décodage
        full_response = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
        
        # Extraction de la réponse générée (enlever le prompt)
        generated_response = full_response[len(formatted_prompt):].strip()
        
        return formatted_prompt, generated_response


def create_app(model_server: ModelServer) -> FastAPI:
    """Crée l'application FastAPI."""
    
    app = FastAPI(
        title="Lumi Model Server",
        description="API pour interagir avec le modèle Lumi entraîné",
        version="1.0.0"
    )
    
    @app.get("/")
    async def root():
        """Endpoint racine avec informations sur le modèle."""
        return {
            "message": "Lumi Model Server",
            "model_device": str(model_server.device),
            "model_parameters": sum(p.numel() for p in model_server.model.parameters()),
            "available_templates": ["chatml", "chat", "instruct", "raw"],
            "endpoints": {
                "generate": "/generate - Génération de texte",
                "health": "/health - Status du serveur"
            }
        }
    
    @app.get("/health")
    async def health():
        """Endpoint de santé du serveur."""
        return {"status": "healthy", "device": str(model_server.device)}
    
    @app.post("/generate", response_model=GenerationResponse)
    async def generate(request: GenerationRequest):
        """
        Endpoint de génération de texte.
        
        Args:
            request: Requête de génération
            
        Returns:
            Réponse générée
        """
        try:
            formatted_prompt, response = model_server.generate(
                prompt=request.prompt,
                max_new_tokens=request.max_new_tokens,
                temperature=request.temperature,
                top_p=request.top_p,
                repetition_penalty=request.repetition_penalty,
                do_sample=request.do_sample,
                template=request.template
            )
            
            return GenerationResponse(
                response=response,
                prompt=request.prompt,
                generation_config={
                    "max_new_tokens": request.max_new_tokens,
                    "temperature": request.temperature,
                    "top_p": request.top_p,
                    "repetition_penalty": request.repetition_penalty,
                    "template": request.template
                }
            )
        
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erreur de génération: {str(e)}")
    
    return app


def interactive_mode(model_server: ModelServer, args):
    """Mode interactif CLI pour discuter avec le modèle."""
    
    print("🤖 Mode interactif Lumi")
    print("=" * 50)
    print(f"Modèle: {args.model_path}")
    print(f"Template: {args.template}")
    print(f"Paramètres: temp={args.temperature}, top_p={args.top_p}")
    print("Tapez 'exit', 'quit' ou Ctrl+C pour quitter")
    print("=" * 50)
    
    try:
        while True:
            # Saisie utilisateur
            try:
                user_input = input("\n👤 Vous: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n\nAu revoir! 👋")
                break
            
            # Commandes spéciales
            if user_input.lower() in ['exit', 'quit', 'q']:
                print("Au revoir! 👋")
                break
            
            if not user_input:
                continue
            
            # Génération de la réponse
            print("🤖 Lumi: ", end="", flush=True)
            
            try:
                _, response = model_server.generate(
                    prompt=user_input,
                    max_new_tokens=args.max_new_tokens,
                    temperature=args.temperature,
                    top_p=args.top_p,
                    repetition_penalty=args.repetition_penalty,
                    do_sample=args.do_sample,
                    template=args.template
                )
                
                print(response)
                
            except Exception as e:
                print(f"❌ Erreur lors de la génération: {e}")
    
    except KeyboardInterrupt:
        print("\n\nAu revoir! 👋")


def main():
    parser = argparse.ArgumentParser(description="Serveur d'inférence Lumi")
    
    # Arguments principaux
    parser.add_argument("--model_path", type=str, required=True,
                       help="Chemin vers le modèle entraîné")
    parser.add_argument("--tokenizer_path", type=str, default=None,
                       help="Chemin vers le tokenizer (optionnel)")
    parser.add_argument("--mode", type=str, default="interactive", 
                       choices=["interactive", "api"],
                       help="Mode d'exécution: interactive ou api")
    
    # Paramètres de génération
    parser.add_argument("--max_new_tokens", type=int, default=100,
                       help="Nombre maximum de nouveaux tokens")
    parser.add_argument("--temperature", type=float, default=0.7,
                       help="Température de génération")
    parser.add_argument("--top_p", type=float, default=0.9,
                       help="Top-p sampling")
    parser.add_argument("--repetition_penalty", type=float, default=1.1,
                       help="Pénalité de répétition")
    parser.add_argument("--do_sample", action="store_true", default=True,
                       help="Utiliser l'échantillonnage")
    parser.add_argument("--template", type=str, default="chatml",
                       choices=["chatml", "chat", "instruct", "raw"],
                       help="Template de formatage des prompts")
    
    # Paramètres API
    parser.add_argument("--host", type=str, default="127.0.0.1",
                       help="Adresse IP pour le serveur API")
    parser.add_argument("--port", type=int, default=8000,
                       help="Port pour le serveur API")
    
    args = parser.parse_args()
    
    # Vérification du modèle
    if not Path(args.model_path).exists():
        print(f"❌ Erreur: Modèle non trouvé à {args.model_path}")
        sys.exit(1)
    
    # Initialisation du serveur de modèle
    try:
        model_server = ModelServer(args.model_path, args.tokenizer_path)
    except Exception as e:
        print(f"❌ Erreur lors du chargement du modèle: {e}")
        sys.exit(1)
    
    # Lancement selon le mode
    if args.mode == "interactive":
        interactive_mode(model_server, args)
    
    elif args.mode == "api":
        print(f"🚀 Démarrage du serveur API sur {args.host}:{args.port}")
        app = create_app(model_server)
        
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="info"
        )


if __name__ == "__main__":
    main()